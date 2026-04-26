# destroy 순서: 이 null_resource 먼저 삭제(→ cleanup 실행) → 노드그룹 → EKS 클러스터
# depends_on 으로 EKS/노드가 살아 있는 동안 kubectl 정리가 수행되도록 보장한다.
# ALB Controller가 만든 로드밸런서·타겟그룹·ENI를 제거해야 VPC destroy가 성공한다.
# 아래 sleep 은 잔여 ENI/ALB 때문에 subnet 삭제가 막히는 걸 줄이기 위함 — 줄이면 destroy 실패율이 올라갈 수 있음.
resource "null_resource" "cleanup_k8s_resources" {
  triggers = {
    cluster_name = var.cluster_name
    region       = var.aws_region
    vpc_id       = var.vpc_id
  }

  depends_on = [
    aws_eks_cluster.main,
    aws_eks_node_group.app,
  ]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    when        = destroy
    environment = {
      EKS_CLUSTER_NAME = self.triggers.cluster_name
      EKS_REGION       = self.triggers.region
      EKS_VPC_ID       = self.triggers.vpc_id
      # AWS CLI v2 기본 pager 비활성화 — destroy 중 멈춤 방지.
      AWS_PAGER = ""
    }
    # HGFS/Windows 등에서 .tf 가 CRLF 일 때 heredoc 이 깨지므로 스크립트 파일 + 실행 시 CR 제거
    command = "tr -d '\\r' < \"${path.module}/scripts/cleanup_k8s_resources_on_destroy.sh\" | bash"
  }
}

# NOTE:
# - 위 cleanup_k8s_resources 는 "EKS/노드가 살아있는 동안" Ingress/SVC 등을 먼저 내리기 위한 선행 정리다.
# - 하지만 실제로는 노드그룹 종료 후에 aws-k8s ENI가 'available' 상태로 남는 타이밍이 발생할 수 있다.
# - 아래 cleanup_vpc_leftovers_post 는 노드그룹/클러스터 삭제 "후"에 한 번 더 ENI 잔재를 지워서
#   subnet 삭제가 막히는 상황을 줄인다.
resource "null_resource" "cleanup_vpc_leftovers_post" {
  triggers = {
    region = var.aws_region
    vpc_id = var.vpc_id
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    when        = destroy
    environment = {
      EKS_POST_REGION = self.triggers.region
      EKS_POST_VPC_ID = self.triggers.vpc_id
      # AWS CLI v2 기본 pager 비활성화 — destroy 중 멈춤 방지.
      AWS_PAGER = ""
    }
    command = "tr -d '\\r' < \"${path.module}/scripts/cleanup_vpc_enis_post_eks_destroy.sh\" | bash"
  }
}

data "aws_iam_policy_document" "eks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

locals {
  # IAM Role/Policy names have length limits; keep stable but avoid hardcoding.
  name_prefix = substr(replace(var.cluster_name, "/[^a-zA-Z0-9+=,.@_-]/", "-"), 0, 32)
}

resource "aws_iam_role" "eks_cluster" {
  name               = "${local.name_prefix}-eks-cluster-role"
  assume_role_policy = data.aws_iam_policy_document.eks_assume.json
}

resource "aws_iam_role_policy_attachment" "eks_cluster" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_eks_cluster" "main" {
  name     = var.cluster_name
  role_arn = aws_iam_role.eks_cluster.arn
  version  = "1.30"

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [var.security_group_id]
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_cluster,
    null_resource.cleanup_vpc_leftovers_post,
  ]
  tags = { Name = var.cluster_name, Environment = var.env }
}

# 노드 그룹 IAM
data "aws_iam_policy_document" "node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eks_node" {
  name               = "${local.name_prefix}-eks-node-role"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
}

resource "aws_iam_role_policy_attachment" "eks_node_worker" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "eks_node_cni" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "eks_node_ecr" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# 노드당 Pod 상한(보통 ENI·보조 IP 개수) 완화 — prefix delegation. 기존 클러스터에 vpc-cni 가 이미 있으면:
#   terraform import 'module.eks.aws_eks_addon.vpc_cni' '<클러스터명>:vpc-cni'
data "aws_eks_addon_version" "vpc_cni" {
  addon_name         = "vpc-cni"
  kubernetes_version = aws_eks_cluster.main.version
  most_recent        = true
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "vpc-cni"
  addon_version               = data.aws_eks_addon_version.vpc_cni.version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  # ENABLE_PREFIX_DELEGATION + VPC Custom Networking 조합 (AWS 권장 best-practice):
  #   - prefix delegation: ENI 1개가 /28(16 IP) 단위로 IP 를 받아 파드 밀도↑
  #   - custom networking: 파드 IP 를 secondary CIDR(100.64.0.0/16) 에서 받아
  #     노드 subnet(10.0.x.x) IP 고갈을 구조적으로 차단
  # 두 기능은 직교(orthogonal)라 같이 켜는 것이 표준.
  configuration_values = jsonencode({
    env = {
      ENABLE_PREFIX_DELEGATION           = "true"
      WARM_PREFIX_TARGET                 = "1"
      AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG = "true"
      # ENIConfig 를 노드의 어떤 라벨로 매칭할지 지정.
      # topology.kubernetes.io/zone 은 kubelet 이 자동 주입하는 표준 AZ 라벨.
      # → ENIConfig 의 metadata.name 은 AZ 풀네임(ap-northeast-2a 등)이어야 한다.
      ENI_CONFIG_LABEL_DEF = "topology.kubernetes.io/zone"
    }
  })

  depends_on = [aws_eks_cluster.main]
}

# ── ENIConfig (VPC Custom Networking) ─────────────────────────────────────
#
# vpc-cni 애드온이 ENIConfig CRD 를 설치한 직후, AZ 당 1개의 ENIConfig 를 생성한다.
# 각 ENIConfig 는 (그 AZ 의 pod subnet, pod ENI 에 붙일 SG 목록) 을 선언하고,
# ipamd 가 노드의 topology.kubernetes.io/zone 라벨과 일치하는 ENIConfig 를
# 자동 선택해 파드에 secondary IP 를 할당한다.
#
# Security Group 선택 근거:
#   - WAS_SG (var.security_group_id)
#       * network 모듈의 db_from_was / cache_from_was 가 이 SG 를 source 로 허용.
#   - EKS cluster SG (aws_eks_cluster.main.vpc_config[0].cluster_security_group_id)
#       * kubelet↔API server, 노드간 pod 통신, CoreDNS 등 control-plane 경로 전반.
#       * root main.tf 의 rds_from_eks_cluster_sg / redis_from_eks_cluster_sg 도 허용.
#   둘을 모두 붙이면 기존 SG 기반 허용 규칙이 pod ENI 에도 그대로 적용된다.
#
# 왜 null_resource + kubectl 인가:
#   - 이 레포는 "Helm/K8s 리소스는 TF provider 대신 쉘 스크립트"로 관리하는 일관 정책.
#   - kubernetes/helm provider 를 새로 configure 하지 않고 기존 패턴 유지.
#
# 재실행 조건:
#   - pod subnet ID / AZ / SG 중 하나라도 바뀌면 triggers.payload_hash 가 변해
#     kubectl apply 재실행. ENIConfig 는 declarative 라 apply 가 곧 upsert.
resource "null_resource" "pod_eni_configs" {
  triggers = {
    cluster_name = aws_eks_cluster.main.name
    region       = var.aws_region
    payload_hash = md5(jsonencode({
      subnets = var.pod_subnet_ids
      azs     = var.pod_subnet_azs
      sgs = [
        var.security_group_id,
        aws_eks_cluster.main.vpc_config[0].cluster_security_group_id,
      ]
    }))
  }

  # vpc-cni 애드온 이후: ENIConfig CRD 가 존재해야 apply 가능.
  # 주의: 반드시 노드 그룹 "이전"에 적용되어야 한다.
  #   custom networking(AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG=true) 가 켜진 상태에서
  #   ENIConfig 가 없이 노드가 올라오면 ipamd 가 secondary IP 풀을 못 만들어
  #   kubelet NotReady → EKS 가 NodeCreationFailure: Unhealthy nodes 로 노드그룹 자체를 실패시킴.
  # ENIConfig 는 declarative CRD 라서 노드가 없어도 kubectl apply 가능하고,
  # 노드가 부팅하는 시점에 ipamd 가 자동으로 참조한다.
  depends_on = [
    aws_eks_addon.vpc_cni,
  ]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    environment = {
      CLUSTER_NAME   = aws_eks_cluster.main.name
      AWS_REGION     = var.aws_region
      POD_SUBNET_IDS = join(",", var.pod_subnet_ids)
      POD_SUBNET_AZS = join(",", var.pod_subnet_azs)
      POD_SG_IDS = join(",", [
        var.security_group_id,
        aws_eks_cluster.main.vpc_config[0].cluster_security_group_id,
      ])
      AWS_PAGER = ""
    }
    command = "tr -d '\\r' < \"${path.module}/scripts/apply_eni_configs.sh\" | bash"
  }
}

# 워커 노드 그룹 — CA scale-up 한도는 scaling_config.max_size (AWS ASG). 태그 없으면 CA가 ASG를 못 찾음.
resource "aws_eks_node_group" "app" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${local.name_prefix}-app-nodes"
  node_role_arn   = aws_iam_role.eks_node.arn
  subnet_ids      = var.subnet_ids
  instance_types  = var.app_node_instance_types
  ami_type        = "AL2023_x86_64_STANDARD"

  scaling_config {
    desired_size = var.app_node_desired_size
    min_size     = var.app_node_min_size
    max_size     = var.app_node_max_size
  }

  update_config { max_unavailable = 1 }

  labels = { role = "app" }

  depends_on = [
    aws_iam_role_policy_attachment.eks_node_worker,
    aws_iam_role_policy_attachment.eks_node_cni,
    aws_iam_role_policy_attachment.eks_node_ecr,
    aws_eks_addon.vpc_cni,
    # custom networking 켠 상태에서는 ENIConfig 가 노드 부팅 전에 깔려 있어야
    # ipamd 가 정상 초기화됨. 없이 시작하면 NodeCreationFailure.
    null_resource.pod_eni_configs,
    null_resource.cleanup_vpc_leftovers_post,
  ]

  # Cluster Autoscaler(Helm autoDiscovery)가 ASG를 찾으려면 두 태그가 ASG에 있어야 함.
  # 없으면 CA는 동작해 보여도 scale-up 대상 그룹이 0이라 노드가 늘지 않고 파드만 Pending.
  tags = merge(
    {
      Name        = "${local.name_prefix}-app-nodes"
      Environment = var.env
    },
    {
      "k8s.io/cluster-autoscaler/enabled"             = "true"
      "k8s.io/cluster-autoscaler/${var.cluster_name}" = "owned"
    }
  )
}

# HPA(Resource)가 cpu/memory utilization 을 받으려면 metrics.k8s.io API 가 필요 — EKS 관리 애드온으로 고정
data "aws_eks_addon_version" "metrics_server" {
  addon_name         = "metrics-server"
  kubernetes_version = aws_eks_cluster.main.version
  most_recent        = true
}

resource "aws_eks_addon" "metrics_server" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "metrics-server"
  addon_version               = data.aws_eks_addon_version.metrics_server.version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  # EKS metrics-server 애드온 configuration_values 스키마에 replicaCount(Helm) 없음 — 레플리카는 post_apply_k8s_bootstrap.sh 에서 kubectl scale
  # QoS: requests=limits → Guaranteed. 죽으면 HPA 메트릭 공급 중단 → read/write-burst HPA 동작 멈춤.
  configuration_values = jsonencode({
    resources = {
      requests = { cpu = "100m", memory = "200Mi" }
      limits   = { cpu = "100m", memory = "200Mi" }
    }
  })

  depends_on = [aws_eks_node_group.app]
}

# ── EBS CSI Driver ─────────────────────────────────────────────────────
# kube-prometheus-stack(Grafana/Prometheus PVC), 기타 stateful 워크로드의 gp3 PVC 프로비저닝에 필요.
# 없으면 StorageClass gp3(ebs.csi.aws.com)가 "WaitForFirstConsumer → 영구 Pending" 으로 남아
# Pod 가 `VolumeBinding: context deadline exceeded` 로 스케줄 실패.
# 형 eks 모듈에는 없음 → 내 FINAL(2f0053a) 에서 복구.
resource "aws_iam_role" "ebs_csi" {
  name = "${local.name_prefix}-ebs-csi-driver-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.eks.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:kube-system:ebs-csi-controller-sa"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "aws-ebs-csi-driver"
  service_account_role_arn    = aws_iam_role.ebs_csi.arn
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
  # QoS: controller 는 PV 프로비저닝 주체라 Guaranteed. node 는 DaemonSet — attach/detach 이벤트만 처리라 가볍게.
  # 기본 limits 가 비정상적으로 컸음(controller 1312Mi) → 현실적 값으로 고정.
  configuration_values = jsonencode({
    controller = {
      resources = {
        requests = { cpu = "100m", memory = "200Mi" }
        limits   = { cpu = "100m", memory = "200Mi" }
      }
    }
    node = {
      resources = {
        requests = { cpu = "50m", memory = "100Mi" }
        limits   = { cpu = "50m", memory = "100Mi" }
      }
    }
  })

  depends_on = [
    aws_eks_node_group.app,
    aws_iam_role_policy_attachment.ebs_csi,
  ]
}

# ALB Controller IAM (Ingress 자동 생성용)
resource "aws_iam_policy" "alb_controller" {
  name   = "${local.name_prefix}-alb-controller-policy"
  policy = file("${path.module}/alb-controller-policy.json")
}

data "aws_caller_identity" "current" {}

locals {
  oidc_issuer = replace(aws_eks_cluster.main.identity[0].oidc[0].issuer, "https://", "")
}

# EKS OIDC endpoint 의 실제 서버 cert 에서 SHA1 thumbprint 를 동적 조회.
# AWS 가 주기적으로 EKS OIDC 서버 cert 를 갱신하는데 하드코딩 값은 stale 이 되어
# IRSA 가 "No OpenIDConnect provider found in your account" 로 영구 파손된다.
# 이 data source 는 apply 시점의 실제 cert fingerprint 를 사용해 drift 를 제거.
data "tls_certificate" "eks_oidc" {
  url = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.main.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks_oidc.certificates[0].sha1_fingerprint]
}

resource "aws_iam_role" "alb_controller" {
  name = "${local.name_prefix}-alb-controller-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.eks.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:kube-system:aws-load-balancer-controller"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "alb_controller" {
  role       = aws_iam_role.alb_controller.name
  policy_arn = aws_iam_policy.alb_controller.arn
}

# Cluster Autoscaler IAM (노드 자동 스케일링용)
resource "aws_iam_policy" "cluster_autoscaler" {
  name = "${local.name_prefix}-cluster-autoscaler-policy"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:DescribeAutoScalingInstances",
          "autoscaling:DescribeLaunchConfigurations",
          "autoscaling:DescribeScalingActivities",
          "autoscaling:DescribeTags",
          "autoscaling:SetDesiredCapacity",
          "autoscaling:TerminateInstanceInAutoScalingGroup",
          "ec2:DescribeLaunchTemplateVersions",
          "ec2:DescribeInstanceTypes",
          "eks:DescribeNodegroup"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role" "cluster_autoscaler" {
  name = "${local.name_prefix}-cluster-autoscaler-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.eks.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:kube-system:cluster-autoscaler"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster_autoscaler" {
  role       = aws_iam_role.cluster_autoscaler.name
  policy_arn = aws_iam_policy.cluster_autoscaler.arn
}

# SQS 접근용 IRSA (reserv-svc, worker-svc 공용)
resource "aws_iam_role" "sqs_access" {
  name = "${local.name_prefix}-sqs-access-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.eks.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
            "${local.oidc_issuer}:sub" = "system:serviceaccount:ticketing:sqs-access-sa"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "sqs_access" {
  name = "${local.name_prefix}-sqs-access-policy"
  role = aws_iam_role.sqs_access.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueUrl",
        "sqs:GetQueueAttributes",
      ]
      Resource = var.sqs_queue_arns
    }]
  })
}

# DB backup CronJob 용 IRSA — mysqldump 결과를 assets 버킷의 backups/ prefix 에 PutObject.
# RDS 자체 backup_retention_period=1 (AWS 내부 1일치)을 보강해 시연/감사용 백업 산출물을 S3 에 남긴다.
resource "aws_iam_role" "db_backup" {
  name = "${local.name_prefix}-db-backup-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.eks.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
            "${local.oidc_issuer}:sub" = "system:serviceaccount:ticketing:db-backup-sa"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "db_backup" {
  # count 는 plan-time known 값만 써야 함. assets_bucket_arn(다른 모듈 output)은 destroy/refresh 에서 unknown 이 되어
  # "Invalid count argument" 가 터짐 → 명시적 bool 플래그로 분리.
  count = var.enable_db_backup_to_assets ? 1 : 0
  name  = "${local.name_prefix}-db-backup-policy"
  role  = aws_iam_role.db_backup.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:PutObject",
        "s3:AbortMultipartUpload",
      ]
      Resource = "${var.assets_bucket_arn}/backups/*"
    }]
  })
}
