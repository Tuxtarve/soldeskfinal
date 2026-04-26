# 네이밍: 설계도 (Public_VPC 웹·WAS, Private_VPC DB) — 단일 VPC에 서브넷/태그로 구분
# DB 계층 서브넷은 태그만 Private_VPC_* (동일 VPC, 피어링 미구성 시 실제 Private_VPC 분리 없음)

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = {
    Name        = "Public_VPC"
    Environment = var.env
    Layer       = "web-was-data-colocated"
  }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags = {
    Name        = "IGW"
    Environment = var.env
  }
}

# 웹 공용 서브넷 (ALB, EKS 노드, 모니터링)
resource "aws_subnet" "public" {
  # EKS(Pod IP) 소비가 커서 /24 두 개만으로는 burst 시 IP 고갈이 쉽게 발생함.
  # (aws-cni: failed to assign an IP address to container)
  # 따라서 public subnet을 4개로 늘려 IP pool을 확장한다.
  count      = 4
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.${count.index}.0/24"
  # IMPORTANT:
  # - EKS 클러스터는 생성 시점에 "서브넷이 속한 AZ의 집합"이 고정된다.
  # - 기존 클러스터가 2a/2b로 만들어졌다면, 이후 업데이트에서도 정확히 그 AZ 집합(2a/2b)만 허용된다.
  # - 그래서 subnet은 늘리되(4개), AZ는 2a/2b 안에서만 번갈아 배치한다.
  availability_zone       = data.aws_availability_zones.available.names[count.index % 2]
  map_public_ip_on_launch = true
  tags = {
    Name                                            = "Public_VPC_Web_Pub_RT_SN${count.index + 1}"
    Environment                                     = var.env
    "kubernetes.io/role/elb"                        = "1"
    "kubernetes.io/cluster/${var.eks_cluster_name}" = "shared"
  }
}

# DB·캐시용 프라이빗 서브넷 (설계도 Private_VPC DB 티어 명명)
resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.${count.index + 10}.0/24"
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags = {
    Name        = "Private_VPC_DB_Pri_RT_SN${count.index + 1}"
    Environment = var.env
    Layer       = "db"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = {
    Name        = "Public_VPC_Web_Pub_RT"
    Environment = var.env
  }
}

resource "aws_route_table_association" "public" {
  count          = 4
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private_db" {
  vpc_id = aws_vpc.main.id
  tags = {
    Name        = "Private_VPC_DB_Pri_RT"
    Environment = var.env
  }
}

resource "aws_route_table_association" "private_db" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private_db.id
}

# ── Pod 전용 Secondary CIDR (EKS VPC Custom Networking) ────────────────────
#
# Why:
# - /24 public 서브넷 4개(이미 2→4 확장됨)에 "노드 primary IP"와 "파드 IP"가
#   섞여서 소비됨. ENABLE_PREFIX_DELEGATION=true 환경에서는 파드마다 /28(16개)
#   prefix 를 잡기 때문에 노드 몇 대만 떠도 Available IPv4 가 폭발적으로 줄어
#   aws-cni 가 `InsufficientCidrBlocks` / "not enough free IPv4 or prefixes"
#   를 뱉고 파드가 ContainerCreating 에서 무한 대기.
#
# Strategy (VPC Custom Networking):
# - VPC 에 RFC6598 CGNAT 대역(100.64.0.0/16) secondary CIDR 추가.
# - 파드 ENI(=노드의 secondary ENI)는 이 대역에서 IP 를 받음.
# - 노드 primary ENI 는 기존 10.0.x.x 유지 → 노드 subnet 의 IP 소모는
#   사실상 "노드 대수" 만큼만 됨 → 고갈 구조적으로 차단.
# - 정작 IP 가 빨리 닳는 파드 쪽은 /16(65k) 대역을 통째로 차지 → 사실상 무제한.
#
# Route policy:
# - pod subnet 은 map_public_ip_on_launch = false (파드 직노출 금지).
# - 외부 egress 는 AWS_VPC_K8S_CNI_EXTERNALSNAT=false (기본값) 로 노드
#   primary ENI(public) 경유 SNAT → pod subnet 의 RT 는 VPC local 만 있어도 충분.
#   단, 코드 간결성 위해 기존 public RT 를 재사용(IGW 경로가 있어도 pod ENI 에
#   public IP 가 없으므로 실제 직접 노출은 일어나지 않음).
#
# Why 100.64.0.0/16 (CGNAT / RFC 6598):
# - 10.x / 172.16.x / 192.168.x 같은 흔한 사설 대역과 충돌 확률이 낮음.
# - AWS 공식 EKS custom networking 가이드의 표준 예시 대역.
# - 본 레포는 VPC 피어링/VPN/온프레미스 연동이 없어 외부 충돌 가능성 0.
#
# Tagging 주의:
# - kubernetes.io/cluster/<name> / kubernetes.io/role/* 태그를 의도적으로 붙이지 않음.
#   (이 태그가 붙으면 ALB 컨트롤러가 서브넷 자동 탐색 때 pod subnet 에도
#   리스너/ENI 붙이려다 실패. 노드 subnet 에만 태그 유지.)

resource "aws_vpc_ipv4_cidr_block_association" "pod" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "100.64.0.0/16"
}

resource "aws_subnet" "pod" {
  count = 2
  # /18 = 16,384 IP. prefix delegation 의 /28 기준 최대 1,024개 prefix 할당 가능.
  # (노드당 수백 파드 × AZ 노드 수) 시나리오를 넉넉히 덮음.
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "100.64.${count.index * 64}.0/18"
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false
  depends_on              = [aws_vpc_ipv4_cidr_block_association.pod]

  tags = {
    Name        = "Pod_Subnet_${count.index + 1}_AZ${count.index + 1}"
    Environment = var.env
    Tier        = "pods"
  }
}

resource "aws_route_table_association" "pod" {
  count          = 2
  subnet_id      = aws_subnet.pod[count.index].id
  route_table_id = aws_route_table.public.id
}

# Web_SG: 퍼블릭 웹·모니터링 (80, 443, 22, icmp)
resource "aws_security_group" "web" {
  name        = "prod-monitoring-sg"
  vpc_id      = aws_vpc.main.id
  description = "EC2 monitoring server security group"

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "Grafana"
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "Prometheus / Alertmanager"
    from_port   = 9090
    to_port     = 9093
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = {
    Name        = "Web_SG"
    Environment = var.env
  }
}

# WAS_SG: EKS 워커 (노드 간 + API, ICMP, WAS SSH는 VPC 내부)
resource "aws_security_group" "was" {
  name        = "prod-eks-sg"
  vpc_id      = aws_vpc.main.id
  description = "EKS worker node security group"

  ingress {
    from_port = 0
    to_port   = 0
    protocol  = "-1"
    self      = true
  }
  ingress {
    description = "Kubernetes API"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }
  ingress {
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = ["10.0.0.0/16"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = {
    Name        = "WAS_SG"
    Environment = var.env
  }
}

# DB_SG: MySQL — 인라인 규칙 제거, 모두 aws_security_group_rule로 관리
resource "aws_security_group" "db" {
  name        = "prod-rds-sg"
  vpc_id      = aws_vpc.main.id
  description = "RDS Aurora SG - allow from EKS only"

  tags = {
    Name        = "DB_SG"
    Environment = var.env
  }
}

resource "aws_security_group_rule" "db_icmp" {
  type              = "ingress"
  description       = "ICMP from VPC"
  from_port         = -1
  to_port           = -1
  protocol          = "icmp"
  security_group_id = aws_security_group.db.id
  cidr_blocks       = ["10.0.0.0/16"]
}

resource "aws_security_group_rule" "db_egress_all" {
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  security_group_id = aws_security_group.db.id
  cidr_blocks       = ["0.0.0.0/0"]
}

resource "aws_security_group_rule" "db_from_was" {
  type                     = "ingress"
  description              = "MySQL from WAS"
  from_port                = 3306
  to_port                  = 3306
  protocol                 = "tcp"
  security_group_id        = aws_security_group.db.id
  source_security_group_id = aws_security_group.was.id
}

resource "aws_security_group_rule" "db_ssh_from_web" {
  type                     = "ingress"
  description              = "SSH from Web_SG"
  from_port                = 22
  to_port                  = 22
  protocol                 = "tcp"
  security_group_id        = aws_security_group.db.id
  source_security_group_id = aws_security_group.web.id
}

# Redis (설계도 외 — Cache_SG): 별도 SG 규칙으로 분리하여 순환 참조 방지
resource "aws_security_group" "cache" {
  name        = "Cache_SG"
  vpc_id      = aws_vpc.main.id
  description = "ElastiCache Redis from WAS only"

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = {
    Name        = "Cache_SG"
    Environment = var.env
  }
}

resource "aws_security_group_rule" "cache_from_was" {
  type                     = "ingress"
  description              = "Redis from WAS"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = aws_security_group.cache.id
  source_security_group_id = aws_security_group.was.id
}

resource "aws_security_group_rule" "cache_from_monitoring" {
  type                     = "ingress"
  description              = "Redis from Monitoring"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = aws_security_group.cache.id
  source_security_group_id = aws_security_group.web.id
}

# 설계도의 DB_Connection_Peering: 두 VPC 분리 시 사용. 현재 단일 VPC이므로 리소스 없음(문서화용 주석).

data "aws_availability_zones" "available" {
  state = "available"
}

# -----------------------------------------------------------------------------
# Destroy-time cleanup guard
#
# Why:
# - VPC/Subnet 삭제가 느리거나 2번 destroy를 해야 끝나는 경우가 잦음.
# - 주로 K8s/ALB 컨트롤러가 남긴 ENI / VPC Endpoint / k8s 보안그룹 등이 원인.
#
# Policy:
# - This runs ONLY at destroy time.
# - It tries to remove obvious leftovers that block VPC deletion.
# - It intentionally avoids touching Terraform-managed SGs (prod-eks-sg, prod-rds-sg, etc.)
#   and focuses on k8s-generated artifacts.
# -----------------------------------------------------------------------------
resource "null_resource" "cleanup_vpc_leftovers_before_destroy" {
  triggers = {
    vpc_id = aws_vpc.main.id
    region = var.aws_region
  }

  depends_on = [
    aws_vpc.main,
  ]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    when        = destroy
    environment = {
      NET_VPC_ID = self.triggers.vpc_id
      NET_REGION = self.triggers.region
      # AWS CLI v2 기본 pager 비활성화 — destroy 중 멈춤 방지.
      AWS_PAGER = ""
    }
    command = "tr -d '\\r' < \"${path.module}/scripts/cleanup_vpc_leftovers_before_destroy.sh\" | bash"
  }
}