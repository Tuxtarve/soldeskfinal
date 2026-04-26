#!/usr/bin/env bash
# terraform destroy 래퍼 스크립트
# EKS/ALB Controller가 Terraform 외부에서 생성한 리소스(EIP, ELB, TG, ENI, SG)를
# 자동 감지·정리하여 VPC/IGW/Subnet 삭제 실패를 영구적으로 방지한다.
#
# 2중 안전장치:
#   1) Terraform null_resource.post_eks_vpc_cleanup — EKS 삭제 후 VPC 삭제 전에 실행
#   2) 이 스크립트의 백그라운드 정리 루프 — terraform destroy 실행 중 주기적으로 고아 리소스 제거
set -uo pipefail

# AWS CLI v2 기본 pager(less/more) 비활성화. 이 스크립트는 SG/ENI/ELB describe·delete
# 호출이 많아 pager 가 한 번이라도 뜨면 destroy 흐름 전체가 멈춘다. 자식 프로세스에
# 상속되도록 export.
export AWS_PAGER=""

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/terraform"

# db_password는 variables.tf에서 default 없이 required로 선언되어 있어, destroy
# 조차도 var 값을 요구한다. destroy는 실제로 그 값을 쓰지 않으므로 호출자가
# 안 넣은 경우 더미 값을 자동 주입한다 (apply 시에는 여전히 외부에서 명시해야 함).
export TF_VAR_db_password="${TF_VAR_db_password:-destroy-dummy}"

MAX_RETRIES=4
REGION=$(terraform output -raw aws_region 2>/dev/null || echo "ap-northeast-2")

# ── ALB data source state 분리 ──────────────────────────────────
# main.tf의 data "aws_lb_listener" / "aws_lb"가 state에 박혀있으면 plan 시
# refresh 단계에서 옛 ALB ARN으로 lookup하다 NotFound로 destroy가 fail함.
# data source는 state에서 빼도 AWS 리소스에 영향 0.
echo "============================================="
echo " ALB data source state 분리"
echo "============================================="
terraform state rm 'data.aws_lb_listener.ingress[0]' 2>/dev/null \
  && echo "  lb_listener data source: state 분리 완료" \
  || echo "  lb_listener data source: state에 없음"
terraform state rm 'data.aws_lb.ingress[0]' 2>/dev/null \
  && echo "  lb data source: state 분리 완료" \
  || echo "  lb data source: state에 없음"
echo ""

# ── VPC ID 확인 ──────────────────────────────────────────────────
get_vpc_id() {
  local vpc_id
  vpc_id=$(terraform state show 'module.network.aws_vpc.main' 2>/dev/null \
    | grep '^\s*id\s*=' | head -1 | sed 's/.*= *"//;s/".*//')
  if [ -n "$vpc_id" ]; then echo "$vpc_id"; return; fi

  vpc_id=$(aws ec2 describe-vpcs --region "$REGION" \
    --filters "Name=tag:Name,Values=Public_VPC" \
    --query 'Vpcs[0].VpcId' --output text 2>/dev/null)
  if [ "$vpc_id" != "None" ] && [ -n "$vpc_id" ]; then echo "$vpc_id"; return; fi

  echo ""
}

# ── K8s 고아 리소스만 정리 (경량 — 백그라운드 루프용) ────────────
cleanup_k8s_orphans() {
  local vpc_id="$1"
  [ -z "$vpc_id" ] && return 0

  # K8s/EKS가 생성한 보안 그룹 (k8s-*, eks-cluster-sg-*)
  local k8s_sgs
  k8s_sgs=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=vpc-id,Values=$vpc_id" "Name=group-name,Values=k8s-*,eks-cluster-sg-*" \
    --query 'SecurityGroups[*].GroupId' --output text 2>/dev/null)

  if [ -n "$k8s_sgs" ] && [ "$k8s_sgs" != "None" ]; then
    echo "[bg-cleanup] 고아 SG 발견: $k8s_sgs"
    for SG_ID in $k8s_sgs; do
      INGRESS=$(aws ec2 describe-security-groups --group-ids "$SG_ID" --region "$REGION" \
        --query 'SecurityGroups[0].IpPermissions' --output json 2>/dev/null)
      if [ "$INGRESS" != "[]" ] && [ -n "$INGRESS" ]; then
        aws ec2 revoke-security-group-ingress --group-id "$SG_ID" --ip-permissions "$INGRESS" --region "$REGION" 2>/dev/null || true
      fi
      EGRESS=$(aws ec2 describe-security-groups --group-ids "$SG_ID" --region "$REGION" \
        --query 'SecurityGroups[0].IpPermissionsEgress' --output json 2>/dev/null)
      if [ "$EGRESS" != "[]" ] && [ -n "$EGRESS" ]; then
        aws ec2 revoke-security-group-egress --group-id "$SG_ID" --ip-permissions "$EGRESS" --region "$REGION" 2>/dev/null || true
      fi
    done
    for SG_ID in $k8s_sgs; do
      echo "[bg-cleanup] SG 삭제: $SG_ID"
      aws ec2 delete-security-group --group-id "$SG_ID" --region "$REGION" 2>/dev/null || true
    done
  fi

  # 고아 ENI 정리
  for ENI_ID in $(aws ec2 describe-network-interfaces --region "$REGION" \
    --filters "Name=vpc-id,Values=$vpc_id" "Name=status,Values=available" \
    --query 'NetworkInterfaces[*].NetworkInterfaceId' --output text 2>/dev/null); do
    echo "[bg-cleanup] ENI 삭제: $ENI_ID"
    aws ec2 delete-network-interface --network-interface-id "$ENI_ID" --region "$REGION" 2>/dev/null || true
  done
}

# ── VPC 내 모든 외부 생성 리소스 정리 (전체 — 사전/사후 정리용) ──
cleanup_vpc() {
  local vpc_id="$1"
  if [ -z "$vpc_id" ]; then
    echo "[cleanup] VPC를 찾을 수 없습니다. 건너뜁니다."
    return 0
  fi
  echo "============================================="
  echo " VPC 정리 시작: $vpc_id"
  echo "============================================="

  # 1) EIP 해제
  echo ">> [1/6] Elastic IP 해제"
  aws ec2 describe-addresses --region "$REGION" \
    --filters "Name=domain,Values=vpc" \
    --query 'Addresses[*].[AllocationId,AssociationId,NetworkInterfaceId]' \
    --output text 2>/dev/null | while read -r ALLOC_ID ASSOC_ID NID; do
    [ -z "$ALLOC_ID" ] && continue
    if [ "$NID" != "None" ] && [ -n "$NID" ]; then
      ENI_VPC=$(aws ec2 describe-network-interfaces --region "$REGION" \
        --network-interface-ids "$NID" \
        --query 'NetworkInterfaces[0].VpcId' --output text 2>/dev/null)
      [ "$ENI_VPC" != "$vpc_id" ] && continue
    fi
    if [ "$ASSOC_ID" != "None" ] && [ -n "$ASSOC_ID" ]; then
      echo "   disassociate $ALLOC_ID ($ASSOC_ID)"
      aws ec2 disassociate-address --association-id "$ASSOC_ID" --region "$REGION" 2>/dev/null || true
    fi
    echo "   release $ALLOC_ID"
    aws ec2 release-address --allocation-id "$ALLOC_ID" --region "$REGION" 2>/dev/null || true
  done

  # 2) ALB / NLB 삭제
  echo ">> [2/6] ALB/NLB 삭제"
  for LB_ARN in $(aws elbv2 describe-load-balancers --region "$REGION" \
    --query "LoadBalancers[?VpcId=='$vpc_id'].LoadBalancerArn" --output text 2>/dev/null); do
    echo "   delete $LB_ARN"
    aws elbv2 delete-load-balancer --load-balancer-arn "$LB_ARN" --region "$REGION" 2>/dev/null || true
  done

  # 3) Classic ELB 삭제
  echo ">> [3/6] Classic ELB 삭제"
  for CLB in $(aws elb describe-load-balancers --region "$REGION" \
    --query "LoadBalancerDescriptions[?VPCId=='$vpc_id'].LoadBalancerName" --output text 2>/dev/null); do
    echo "   delete $CLB"
    aws elb delete-load-balancer --load-balancer-name "$CLB" --region "$REGION" 2>/dev/null || true
  done

  # 4) Target Group 삭제
  echo ">> [4/6] Target Group 삭제"
  for TG_ARN in $(aws elbv2 describe-target-groups --region "$REGION" \
    --query "TargetGroups[?VpcId=='$vpc_id'].TargetGroupArn" --output text 2>/dev/null); do
    echo "   delete $TG_ARN"
    aws elbv2 delete-target-group --target-group-arn "$TG_ARN" --region "$REGION" 2>/dev/null || true
  done

  echo "   (ENI 해제 대기 20초)"
  sleep 20

  # 5) ENI 분리 → 삭제
  echo ">> [5/6] ENI 분리 및 삭제"
  for ENI_ID in $(aws ec2 describe-network-interfaces --region "$REGION" \
    --filters "Name=vpc-id,Values=$vpc_id" \
    --query 'NetworkInterfaces[?Attachment.DeviceIndex!=`0` || !Attachment].NetworkInterfaceId' \
    --output text 2>/dev/null); do
    ATTACH_ID=$(aws ec2 describe-network-interfaces --region "$REGION" \
      --network-interface-ids "$ENI_ID" \
      --query 'NetworkInterfaces[0].Attachment.AttachmentId' --output text 2>/dev/null)
    if [ "$ATTACH_ID" != "None" ] && [ -n "$ATTACH_ID" ]; then
      echo "   detach $ENI_ID"
      aws ec2 detach-network-interface --attachment-id "$ATTACH_ID" --force --region "$REGION" 2>/dev/null || true
    fi
  done
  sleep 10
  for ENI_ID in $(aws ec2 describe-network-interfaces --region "$REGION" \
    --filters "Name=vpc-id,Values=$vpc_id" "Name=status,Values=available" \
    --query 'NetworkInterfaces[*].NetworkInterfaceId' --output text 2>/dev/null); do
    echo "   delete $ENI_ID"
    aws ec2 delete-network-interface --network-interface-id "$ENI_ID" --region "$REGION" 2>/dev/null || true
  done

  # 6) non-default 보안그룹 전체 정리
  echo ">> [6/6] non-default 보안그룹 전체 정리"
  K8S_SGS=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=vpc-id,Values=$vpc_id" \
    --query "SecurityGroups[?GroupName!='default'].GroupId" \
    --output text 2>/dev/null)
  for SG_ID in $K8S_SGS; do
    echo "   revoke rules $SG_ID"
    INGRESS=$(aws ec2 describe-security-groups --group-ids "$SG_ID" --region "$REGION" \
      --query 'SecurityGroups[0].IpPermissions' --output json 2>/dev/null)
    if [ "$INGRESS" != "[]" ] && [ -n "$INGRESS" ]; then
      aws ec2 revoke-security-group-ingress --group-id "$SG_ID" --ip-permissions "$INGRESS" --region "$REGION" 2>/dev/null || true
    fi
    EGRESS=$(aws ec2 describe-security-groups --group-ids "$SG_ID" --region "$REGION" \
      --query 'SecurityGroups[0].IpPermissionsEgress' --output json 2>/dev/null)
    if [ "$EGRESS" != "[]" ] && [ -n "$EGRESS" ]; then
      aws ec2 revoke-security-group-egress --group-id "$SG_ID" --ip-permissions "$EGRESS" --region "$REGION" 2>/dev/null || true
    fi
  done
  for SG_ID in $K8S_SGS; do
    echo "   delete $SG_ID"
    aws ec2 delete-security-group --group-id "$SG_ID" --region "$REGION" 2>/dev/null || true
  done

  echo "============================================="
  echo " VPC 정리 완료"
  echo "============================================="
}

# ── 메인 ─────────────────────────────────────────────────────────
VPC_ID=$(get_vpc_id)

# tfvars의 alb_listener_arn / frontend_callback_domain을 미리 빈값으로 reset.
# main.tf의 data "aws_lb_listener" 가 옛 ARN을 lookup하다 NotFound로 destroy
# plan을 fail시키는 데드락 방지. setup-all.sh가 다음 apply 후반부에 새 ARN을
# 자동으로 다시 박는다. -var override만으로는 retry loop / cached state 등의
# 경로에서 작동 안 할 수 있어 tfvars 자체를 사전 reset.
TFVARS="$ROOT/terraform/terraform.tfvars"
if [ -f "$TFVARS" ]; then
  sed -i 's|^alb_listener_arn.*|alb_listener_arn = ""|' "$TFVARS" 2>/dev/null || true
  sed -i 's|^frontend_callback_domain.*|frontend_callback_domain = ""|' "$TFVARS" 2>/dev/null || true
  echo "tfvars: alb_listener_arn / frontend_callback_domain pre-reset"
fi

# 1차 사전 정리
cleanup_vpc "$VPC_ID"

attempt=1
while [ $attempt -le $MAX_RETRIES ]; do
  echo ""
  echo "=== terraform destroy 시도 $attempt / $MAX_RETRIES ==="

  # 백그라운드 정리 루프 시작 (30초마다 고아 SG/ENI 제거)
  # terraform destroy가 VPC 삭제에서 hang할 때 이 루프가 차단 리소스를 제거해줌
  (
    sleep 60  # EKS 삭제 완료될 시간 확보
    while true; do
      cleanup_k8s_orphans "$VPC_ID" 2>/dev/null
      sleep 30
    done
  ) &
  BG_PID=$!

  # alb_listener_arn을 빈 값으로 override → main.tf의 data "aws_lb_listener"
  # 가 count=0이 되어 stale ARN을 lookup하지 않음. ALB는 cleanup_vpc 단계에서
  # 이미 삭제됐으니 lookup하면 무조건 NotFound로 destroy가 fail함.
  # -auto-approve는 필수: destroy.sh는 tee로 출력을 파이프하는데, 백그라운드
  # cleanup loop이 stdout을 계속 채우면 terraform의 confirm prompt가 묻혀
  # 사용자가 입력 못 하는 교착 상태가 발생함.
  if terraform destroy -auto-approve -var="alb_listener_arn=" "$@" 2>&1 | tee /tmp/tf_destroy_output.log; then
    kill $BG_PID 2>/dev/null; wait $BG_PID 2>/dev/null
    echo "=== destroy 완료 ==="
    rm -f /tmp/tf_destroy_output.log

    # 다음 apply에서 옛 ALB ARN으로 인한 data source lookup 실패 방지.
    # alb_listener_arn / frontend_callback_domain은 setup-all.sh가 새 ALB
    # 만든 후 다시 자동 채워준다.
    TFVARS="$ROOT/terraform/terraform.tfvars"
    if [ -f "$TFVARS" ]; then
      sed -i 's|^alb_listener_arn.*|alb_listener_arn = ""|' "$TFVARS" 2>/dev/null || true
      sed -i 's|^frontend_callback_domain.*|frontend_callback_domain = ""|' "$TFVARS" 2>/dev/null || true
      echo "tfvars: alb_listener_arn / frontend_callback_domain reset"
    fi

    exit 0
  fi

  kill $BG_PID 2>/dev/null; wait $BG_PID 2>/dev/null
  echo "=== destroy 실패 ==="

  if [ $attempt -lt $MAX_RETRIES ]; then
    FAILED_VPC=$(grep -oP 'vpc-[0-9a-f]+' /tmp/tf_destroy_output.log 2>/dev/null | head -1)
    CLEANUP_TARGET="${FAILED_VPC:-$VPC_ID}"

    if [ -n "$CLEANUP_TARGET" ]; then
      echo ""
      echo "=== 실패 원인 리소스 재정리 후 재시도 ==="
      cleanup_vpc "$CLEANUP_TARGET"
    fi

    WAIT=$((30 * attempt))
    echo "=== ${WAIT}초 대기 후 재시도 ==="
    sleep "$WAIT"
  fi

  attempt=$((attempt + 1))
done

rm -f /tmp/tf_destroy_output.log
echo "=== destroy ${MAX_RETRIES}회 모두 실패했습니다. ==="
echo "수동 확인: aws ec2 describe-addresses / describe-network-interfaces --filters Name=vpc-id,Values=$VPC_ID"
exit 1
