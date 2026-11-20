# 驗證狀態

Git／SOPS（2026-11-20）：實際主機連線、App 與 repo 設定已加密，明文原檔
只留在忽略的本機備份。SOPS 解密比對、Ansible inventory 載入與原始值一致；
解密後的 App 路徑、ProxyJump 與 controller JSON 已用 Ansible templating 驗證。
竄改密文後 Ansible 拒絕載入。38 項本機測試及 12 份 playbook 語法檢查通過。
私鑰、token 與敏感檔名檢查針對 Git index／history 執行，不將命中值輸出。

目前狀態（2026-11-20）：使用者確認 GitHub App 自動派工的 job 已完成。
host 日誌顯示 runner_id=3、候選 job=136197174717 的 VM 於 17:18:13 啟動，
17:19:45 session 回收；後續診斷沒有活動 VM、runtime session 或 auto-state.json。
本機新增唯讀 GitHub 狀態診斷，區分已完成的 job、離線 runner 與仍在等待的工作。
32 項本機測試、診斷 Python 編譯及 automation-status.yml Ansible 語法檢查通過。
跨重啟、連續派工與故障情境仍需實機驗收。

以下保留各階段的驗證記錄；「尚待」描述為當時進度。

App 實機讀取預檢已到達 workflow 檔名核對，使用者確認 repo 實際為
.github/workflows/smoke.yml。Ansible 清單與 controller fallback 預設已同步，
預檢錯誤現會列出 Missing／Available 路徑，不會自動放寬政策。
29 項本地測試與 automation.yml Ansible 語法檢查通過，尚待重跑部署。

自動化增補：已建立 GitHub App JIT controller、rc.d 服務與 automation.yml。
App ID 與 Installation ID 與 Documents 私鑰路徑已設定。
私鑰僅做離線 RSA 格式檢查，沒有輸出內容；尚未向 GitHub 實際換 token。
27 項本地測試通過：含真實 RSA JWT 簽章／驗章、repo-scoped API grant、
排隊工作篩選、空 VM 清單、所有權／失敗清理／正確順序，以及 auto seed 不洩漏。
Python compile、host shell／rc script shell 語法、auto cloud-config JSON/YAML
解析與 guest bash wrapper 語法、automation.yml Ansible 語法檢查通過。
OpenBSD rc.d 的 rc_bg／rc_reload 已依官方要求設在 source rc.subr 之後。
尚待使用者執行 automation.yml 的 App API 預檢及第一個 JIT job；並未遠端部署。

Schema 錯誤已由 guest 證實為 users.1.groups 空陣列，network-config 有效。
新增 repair-cloud-init.yml，在現有 guest 原生驗證修正模板後才清理 instance
state 並正常關機，停機後換 seed，再用相同磁碟啟動。尚待實機執行確認。

Cloud-init 最新診斷：guest 已讀取 NoCloud，但回報 schema validation warning／
degraded done。官方 schema 確認 users[].groups 的 array 不允許空陣列。
建置與 job 模板已將 groups: [] 改為 groups: ''，並將 job sudo: false 改為 null
避免 deprecated 值。新建置的 prepare script 在執行前使用 cloud-init schema
--system 驗證。現有 guest 尚需 schema 明細及直接 marker 檢查；status rc=2
會使原先 && 後的 READY 檢查跳過，不等於 marker 一定不存在。

最新：實機 RAW 中轉與 native qcow2 建立已完成，vmd 載入 build.vm.conf
成功，但 start ci-build 回傳 ENOENT。新增 resume-build-vm，檢查實際路徑、
缺少才建立 tap8 character device，啟動失敗時自動收集 vmd 日誌。
根因尚待診斷證實，不將 ENOENT 直接當成某一特定檔案缺少。
resume-build-vm、resume-cloud-build、activate 三份 playbook 語法檢查通過。

本地環境不是 OpenBSD，無法執行 vmd、pfctl 或實際 guest 開機。

已執行：

- POSIX shell 語法檢查：`sh -n openbsd/run-vm.sh`。
- 非 OpenBSD 環境拒絕執行、錯誤參數拒絕執行。
- 回收流程的 mock 測試：正常停止、host 逾時強制停止、拒絕既有 VM、
  停止失敗保留磁碟。
- 對照官方手冊檢查 vmctl、vm.conf 的參數。

部署前仍需完成 README 第 5 節的實機驗收，尤其 pf 規則整合、guest DHCP、
serial 安裝與 Forgejo 版本相容性。pf 範本不是已在你主機驗證的防火牆配置。

## Ansible 增補

- inventory：ci-admin@192.0.2.10；doas 互動密碼；GitHub OWNER/REPO。
- 6 個 Ansible YAML 檔通過 YAML parser；這不等於 Ansible syntax-check。
- SSH 連線被本地執行環境以 Operation not permitted 阻擋，沒有進入認證階段。
- Ansible 套件下載受 DNS 限制，尚無法執行 ansible-playbook。
- 尚未遠端修改、啟動 VM 或 reload 防火牆。

後續進度：Ansible 已安裝，四份 playbook（包含 activate.yml）通過
ansible-playbook --syntax-check。使用者提供的 preflight 成功確認 host 版本、
Python、VMX/EPT、空間與現有 pf 規則。新增保留 SSH 規則的啟用流程和
180 秒防火牆回復；尚未實際套用或驗證 guest 網路隔離。

實機首次部署停在 vmd -n：舊 VM 定義參照尚不存在的 job.qcow2。
已改為不宣告磁碟的 ci-template；腳本建立衍生磁碟後才用 -t/-d 建立 instance。
6 項本地 mock／回歸測試、shell 語法與 activate.yml Ansible 語法檢查通過。
尚待使用者重跑確認 OpenBSD 原生 vmd 驗證與後續啟用結果。

後續實機已通過 diskless template 驗證、pf 完整配置驗證、套用與新的 SSH
重連，且已取消 rollback。vmd start 返回成功，但緊接的 vmctl reload 得到
Connection refused。新增 resume-vmd.yml：檢查控制介面就緒、失敗時收集
daemon 日誌；不重跑防火牆。resume 與 activate playbook 語法檢查通過。
vmd 尚未確認健康；原因待實機診斷，不假設只是啟動延遲。

實機日誌現已確認根因：vmd 註冊無 kernel/disk/cdrom 的 ci-template 時退出；
vmd -n 並未涵蓋該 runtime 檢查。已取消常駐 template，改為服務啟動時無 CI VM，
建立 job 磁碟後才驗證／load runtime 配置。resume-vmd 會更新 managed block、
script 與 runtime config，不碰防火牆。6 項本地回歸／mock 測試與兩份 Ansible
語法檢查通過，仍待實機確認服務可用。

實機確認 vmd(ok)、所有子程序存在、vmctl 可輸出表頭，證明服務已可通訊。
誤判來自 vmctl 空清單的 rc=1。Ansible 和 shell 已限定接受「stderr 空白＋
完整空表頭＋rc=1」，其他錯誤保留。先以真實空清單語意重現測試失敗，再修正；
8 項回歸測試、shell 語法和 resume-vmd Ansible 語法檢查通過。
額外嘗試本地 Ansible assertion 執行受到 Local RPC server 無法啟動限制，
未列為通過；仍需實機重跑健康檢查確認。

Cloud-init 建置增補：cloud-build.yml、finalize-image.yml 通過 Ansible 語法檢查，
建置／每 job 兩份模板通過 Jinja 渲染與 YAML 解析，內嵌 shell 通過 sh -n，
兩份 MAC 網路配置已解析檢查。8 項 lifecycle 測試通過（包括每 job seed ISO
建立後才驗證 VM）。未執行 cloud-init schema 或 OpenBSD／Ubuntu 首次開機驗收。

實機來源下載與 seed ISO 建立成功，vmctl 轉換因 compressed clusters unsupported
失敗。新增 QEMU 離線格式正規化、原始資料比對、native vmctl 擴展及獨立續跑。
12 項本地 mock 測試與 cloud-build／resume-cloud-build Ansible 語法檢查通過；
尚未實際執行 qemu-img 或驗證 guest 開機。

實機 QEMU 解壓與 compare 成功；vmctl 仍拒絕 compat=0.10 header 的 refcount。
已對照 OpenBSD vioqcow2.c 的 reforder 檢查，並改為 QEMU → sparse RAW →
vmctl 原生 qcow2。更新 mock 的 metadata 拒絕條件，先重現測試失敗，再修正。
