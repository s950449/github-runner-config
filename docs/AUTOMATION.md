# GitHub App 自動派工：OpenBSD 8.0 單機控制器

目標 repo：OWNER/REPO。使用既有唯讀基底 github.qcow2，不重新建置 OS。
沒有 job 時不維持常駐 runner；每 60 秒輪詢，有符合條件的排隊工作才啟動 VM。

```text
GitHub workflow job 排隊
  → host App JWT → 短效 installation access token
  → 建立單次 JIT runner configuration
  → 私有 NoCloud seed → 一次性 Linux VM
  → 非 root runner 執行一個 job
  → guest 關機 → host 清理磁碟／seed／runner registration
```

## 權限與設定

App ID、Installation ID 與本機私鑰路徑已填入
inventory/group_vars/all.sops.yml，使用 SOPS 加密後提交。
修改時使用 `sops edit`；解密方式見 [Git 與 SOPS 設定](SECRETS.md)。
私鑰的 RSA 格式已離線檢查，不曾輸出 PEM 內容。

GitHub App 只安裝至 agent repo，權限：

- Actions：Read（查看 runs、jobs、workflow 清單）。
- Administration：Read and write（GitHub repo JIT runner API 必需）。
- Metadata：GitHub 自動授予的 Read。

不需要 Contents Write、Issues、Pull requests 或雲端權限；不需要 webhook。
App JWT 有效期少於 10 分鐘，installation token 只請求 agent repo 與上述權限，
快取 45 分鐘並在 HTTP 401 時重新換發。App 私鑰與 API token 都不進 guest。

私鑰由 Ansible no_log／無 diff 複製至 /etc/ci-runner/app.pem，root:wheel 0600；
父目錄 0700。JSON policy 也設 0600。App key 輪替時先停止控制器，再部署新 key。

## 部署

先等目前手動工作完成，停止手動 ci-runner VM，讓管理腳本正常回收：

```sh
# 僅在 ci-runner 仍執行時使用；不要中斷正在進行的工作。
ssh -t ci-admin@192.0.2.10 'doas vmctl stop -fw ci-runner'
```

確認沒有手動 VM、runtime/lock 或 build.lock，再在本機 ansible 目錄執行：

```sh
ansible-playbook playbooks/automation.yml --ask-become-pass
```

playbook 不改防火牆，不重建基底。它會：

1. 確認 OpenBSD、vmd、基底、tap8 和容量狀態。
2. 部署控制器、支援 auto session 的 VM 腳本、root-only App key 與 policy。
3. 用本機 Python 檢查 policy／檔案權限，再實際向 GitHub 換 token 做讀取預檢。
4. 確認 workflow allowlist 的檔案確實存在。
5. 安裝並啟動 rc.d ci_controller，設為開機啟動。

第一次讀取預檢不建立 runner 或 VM；啟動服務後，符合條件的工作會自動執行，
不再逐次詢問。若目前已有排隊工作，可能立刻啟動第一個 VM。
JIT 建立所需的 write 權限，必須在第一個實際 job 才能驗證。

若想先只部署並做 API 預檢：

```sh
ansible-playbook playbooks/automation.yml --ask-become-pass -e ci_controller_enabled=false
```

之後在 OpenBSD：`doas rcctl enable ci_controller`、`doas rcctl start ci_controller`。
更新版本前請先停止服務；playbook 會拒絕替換正在執行的控制器。

## Workflow 設定

預設啟機條件只包含 `.github/workflows/smoke.yml`。若你的檔名不同，
修改 ci_controller_workflows。--check 的「Workflow allowlist does not match」
表示清單與 repo 不符，尚未啟動 VM。

```yaml
ci_controller_workflows:
  - .github/workflows/smoke.yml
  - .github/workflows/test.yml
```

job 的 runs-on 使用：

```yaml
runs-on: [self-hosted, Linux, X64, openbsd-vmm]
permissions: {}
timeout-minutes: 10
```

checkout 的 job 再給 contents: read，避免不需要的 write 權限。
允許候選事件為 push、workflow_dispatch、pull_request，且 head_repository
必須是 OWNER/REPO。pull_request_target、workflow_run 與 fork 來源不觸發啟機。
控制器也會檢查正在進行的 run，以便找到其中等待中的下游 job。

**啟機 allowlist 不是 GitHub runner 的授權邊界。** GitHub 會把同 repo 的任意
相符 labels、已允許執行的 job 派到在線 runner；不是控制器指定某個 job ID。
因此所有使用這些 labels 的 workflow 都必須納入 repo 的 PR／workflow 控管。
不要把 labels 或清單當成可隔離發布權限的機制。發布使用不同 runner／憑證。

## 自動模式的 guest

controller 以 JSON cloud-config（有效 YAML）產生單次 seed，包含 JIT capability
與 root 擁有的 systemd unit；沒有 App key、installation token、管理用 SSH key。
JIT capability 不是可管理 repo 的 API token，只用來啟動該單次 runner。

runner 以 gha-runner 執行，沒有 sudo；啟動時讀取並刪除 JIT seed file。
systemd 的固定 ExecStopPost 由 guest root 關機，job 不能改寫此 unit。
runner／job 最長約 55 分鐘，host VM 最長 60 分鐘。
若開機或接單超過 300 秒、runner 消失、服務停止，host 會停止該 session 並回收。
連續失敗採退避，最長 15 分鐘，避免一直重建失敗的 VM。

自動模式不提供 guest SSH 登入公鑰；手動模式仍可使用原來的 job seed 公鑰。
兩模式使用同一 runtime/lock，不同時執行。

## 驗收與觀察

在 GitHub 手動執行 smoke.yml workflow，預期：

1. job 排隊，最多約一分鐘後出現 ci-vmm-... runner。
2. 自動建立 VM、完成 JIT 設定，workflow 開始執行。
3. 測試結束後 VM 自動關機、runner 取消註冊，job disk 和 seed 消失。
4. 再執行一次，建立新的乾淨 VM，沒有沿用前一次工作目錄。

OpenBSD 上的觀察指令：

```sh
doas rcctl check ci_controller
doas tail -n 100 /var/log/daemon
doas vmctl status -r
doas ls -la /home/vmm-ci/runtime
```

沒有排隊工作時，GitHub 沒有在線的自架 runner 是正常狀態。
2026-11-20 使用者確認自動派工的 job 已完成；host 日誌顯示該次 VM 成功啟動，
約 92 秒後回收，診斷時已無活動 VM、session 或 job 磁碟殘留。
每次 job 使用不同的 ci-vmm-... 名稱，不需要讓上一個 runner 持續在線。
rcctl check 只表示程序在執行；API 錯誤會記錄於 daemon log，不代表私鑰已驗證成功。
如需不啟機的 API 檢查：

```sh
doas /usr/local/bin/python3 /usr/local/libexec/ci-controller.py /etc/ci-runner/config.json --check
```

本機 ansible 目錄也可一次取得 host 狀態、GitHub runner 和最近 20 筆 run 中
尚未完成的工作；指定 job ID 可查看該 job 的實際狀態與結論：

```sh
ansible-playbook playbooks/automation-status.yml --ask-become-pass \
  -e ci_diagnostic_job_id=136197174717
```

此診斷不派工、不建立 JIT runner、不改服務或 VM；會用 App 換取短效 token
進行 API 讀取，輸出只包含挑選過的狀態欄位。

GitHub 保存正常 job 日誌；此版不另行匯出 guest `_diag` 原始診斷檔，銷毀 VM 後
這些本地診斷檔不保留。host 只記錄生命週期／API 錯誤，不記錄 JIT 或 App token。

## 停止與故障恢復

```sh
doas rcctl stop ci_controller
doas rcctl disable ci_controller
```

停止控制器可能中斷它管理中的 job，請先等待工作結束。停止時會通知 VM 管理
腳本回收，rc timeout 設為 120 秒。不要直接刪 runtime lock 或活動磁碟。

auto-state.json 只保存 session、runner ID／name、候選 job ID，不保存 token。
控制器重啟後只清理與 session marker 相符的資源；前一個 manager 還在執行時，
等待它結束，不殺掉未知程序。VM 無法確認停止時保留磁碟／lock，不繼續派工。
API delete 暫時失敗則保留 state，稍後重試；不會先刪除仍在使用的 registration。
API orphan 查詢只比對該 session 的唯一完整 runner name，不用前綴批次刪除。

若突然斷電發生在建立 lock、寫入 state 的極小間隔，可能留下無法判定歸屬的
lock。此時會停止啟機，需人工核對 VM／磁碟／state；不猜測並清空所有資源。
API 突然失敗時也可能留下 GitHub 離線記錄，應依 session 記錄人工核對。

## 驗證範圍與來源

已做本地 RSA JWT 實際簽章／驗章、repo-scoped token grant、工作篩選、
VM state 解析、session ownership、停止失敗保留資源、清理順序與 seed 邊界測試。
Ansible 語法、Python／shell 語法可本地驗證；真正的 OpenBSD rc.d、GitHub App
權限、JIT API 與 guest systemd／cloud-init 已隨使用者確認的首個自動 job 完成驗收。
跨重啟恢復、連續多次派工及故障情境尚未實機驗收。
控制器需要 root 管理 vmd 和磁碟，是可信控制層；非 root job 僅存在於 VM。

- [GitHub JIT API 與 Administration 權限](https://docs.github.com/en/rest/actions/self-hosted-runners)
- [Workflow jobs API 與 Actions Read](https://docs.github.com/en/rest/actions/workflow-jobs)
- [GitHub App JWT](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
- [OpenBSD rc.d](https://man.openbsd.org/rc.d.8)
