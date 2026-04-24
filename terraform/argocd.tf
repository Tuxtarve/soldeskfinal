# ArgoCD Application manifest 를 terraform 이 렌더한다.
#
# 왜 이렇게 바뀌었는지:
#   팀원이 fork 로 받아 쓸 때 argocd/application.yaml 의 repoURL 이
#   내(sxk34) 리포에 하드코딩돼 있어, prepare.sh 가 sed + auto-commit + push 로
#   팀원 fork 쪽 git 에 덮어써야 했다. push 권한/네트워크/브랜치 상황에 따라
#   실패가 잦았고, ArgoCD 는 여전히 내 리포를 추적하는 사고가 났다.
#
# 이제는 terraform apply 시 var.github_repo + var.argocd_target_revision 을 기반으로
# argocd/application.rendered.yaml 을 생성한다. install-argocd.sh 가 그 파일을
# kubectl apply 하므로 git 에 커밋·push 할 필요가 없다.
resource "local_file" "argocd_application" {
  filename = "${path.root}/../argocd/application.rendered.yaml"
  content = templatefile("${path.root}/../argocd/application.yaml.tpl", {
    repo_url        = "https://github.com/${var.github_repo}.git"
    target_revision = var.argocd_target_revision
  })

  file_permission      = "0644"
  directory_permission = "0755"
}
