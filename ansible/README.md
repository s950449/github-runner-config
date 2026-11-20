# Ansible：192.0.2.10 的 GitHub runner 基礎設施

## 已有基底：部署自動派工

App ID／Installation ID／本機私鑰路徑已填入 SOPS 加密 group vars。
實際 IP／SSH 帳號保存在加密 host vars；本文連線命令使用示例值。
安裝 requirements.yml 中的 community.sops 後，由 vars plugin 自動解密。
解密身分、編輯與備份方式見 [Git 與 SOPS 設定](../docs/SECRETS.md)。
先等待並回收手動 VM，再執行：

```sh
ansible-playbook playbooks/automation.yml --ask-become-pass
```

會部署 GitHub App JIT 控制器並啟用服務，不改防火牆或重建基底。
完整政策、驗收與停止方式見 [自動化手冊](../docs/AUTOMATION.md)。

目標：OpenBSD 8.0 amd64，SSH user 由加密 inventory 提供，需要密碼的 doas。
GitHub repo：https://github.com/OWNER/REPO 。不部署 Forgejo。

## 執行環境與邊界

這裡的控制端無法連到你的 LAN：SSH 回報 `Operation not permitted`。
使用者已從自己的控制端完成部署；後續操作仍需在能連到實際主機的控制端執行。
控制端安裝 Ansible，OpenBSD 不需安裝 Ansible agent，只需 Python 3。

```sh
# 在本專案根目錄執行，控制端建議 Python 3.12 或 3.13。
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python 'ansible==13.0.0'
cd ansible
../.venv/bin/ansible-galaxy collection install -r requirements.yml
```

Ansible 13.0.0 為固定控制端版本；doas plugin 屬於 community.general。
部署時不修改 doas.conf、不建立 nopass 規則、不儲存密碼或 PAT。
先用 SSH 確認 host fingerprint（和主機 console 核對），不要停用 host key checking：

```sh
ssh ci-admin@192.0.2.10
```

## 1. 只讀預檢

```sh
../.venv/bin/ansible-playbook playbooks/preflight.yml --ask-become-pass
```

這個 playbook 使用 raw，不需要遠端 Python，不修改系統。
若 SSH 本身使用密碼，另外加 `--ask-pass`，並依 Ansible 要求安裝 sshpass；
建議使用 SSH key 且不 forward agent。

預檢應確認 OpenBSD 8.0、VMX/EPT、/home 空間、tap8 是否已使用、原有
vm.conf／pf.conf，以及可用 Python。缺少 /etc/firmware/vmm-bios 時，先在
host 用 `doas fw_update` 安裝；host playbook不自動更新全機韌體或 OS。

若沒安裝 Python，在 host 查詢並選擇 Ansible core 支援的 Python package：

```sh
pkg_info -Q python
# 例如 package repository 有 Python 3.13 時：
doas pkg_add -I 'python%3.13'
```

host playbook會自動偵測 /usr/local/bin/python3 或版本化的 Python 3 路徑。

## 2. 檢查變更，再部署 host 檔案

### 已依 192.0.2.10 的預檢結果新增啟用流程

已確認 OpenBSD 8.0、VMX/EPT、/usr/local/bin/python3、/home 109GB 可用，
未看到既有 vm.conf／tap8。pf 保留 iwx0 和 em0 的 SSH 入站規則。

在 ansible 目錄執行：

```sh
ansible-playbook playbooks/activate.yml --syntax-check
ansible-playbook playbooks/activate.yml --ask-become-pass
```

activate.yml 會先執行 host.yml，再保留原檔加入 CI 管理區塊，建立 tap8／ci
group、整合 NAT 與隔離規則、設定 forwarding、啟用 vmd。VM 保持 disable，
不會立即開始執行工作。這是針對已提供設定的啟用流程，不是通用路由器部署。
若 tap8 已被其他用途使用，或缺少必要的既有 SSH 規則，會停止。

套用前會用 pfctl 驗證完整 candidate；預留 180 秒 rollback，成功重新建立
SSH 連線才取消回復。交易目錄保留於 /home/vmm-ci/runtime/pf-rollback-*，
包含原本 pf.conf；逾時會恢復該檔並 reload。此機制依賴 host 保持運作，
不涵蓋斷電、重開機，亦不回復 vm.conf／forwarding 等其他設定。
SSH 驗證過程若再次詢問 doas 密碼，請在期限內完成。

目前只有原始 pf.conf 已被預檢提供；不要在此流程中手動編輯 include。
若先前的交易留下 pending，先等超過 180 秒並檢查主機狀態，再重跑。
不會 flush 原有 state；要在網路驗收前確認沒有舊 guest 連線 state。

若 host.yml 在 vmm-bios 檢查失敗，先在 OpenBSD 執行 `doas fw_update`，再重跑。
pf 的實機語法驗證如失敗，會在載入之前停止；貼回錯誤供修正。

以下保留僅部署候選檔的流程，適用於想先查看變更的情況：

```sh
../.venv/bin/ansible-playbook playbooks/host.yml --syntax-check
../.venv/bin/ansible-playbook playbooks/host.yml --check --diff --ask-become-pass
../.venv/bin/ansible-playbook playbooks/host.yml --diff --ask-become-pass
```

此步驟會：

- 建立 root:wheel、0700 的 /home/vmm-ci、base、runtime。
- 安裝 /usr/local/sbin/ci-run-vm；複製前檢查 shell 語法。
- 放入 vm.conf.candidate、pf-ci.conf.candidate；VM 配置由 vmd 驗證，pf 於啟用時
  在 ci group 建立後驗證完整配置，避免尚無介面時 standalone 驗證誤判。
- 如果明確指定 ci_base_image_src，可複製乾淨基底；既有基底拒絕覆寫。

此步驟不會：啟動 VM、reload 防火牆、啟用 forwarding 或自動合併 vm.conf。
因為既有 pf 設定尚未讀取，無法僅根據 IP 安全地決定 include 的位置與 NAT。
已完成預檢者可使用上方 activate.yml 啟用；不是每次執行都要求重新批准。

手動啟用時依根目錄 README 第 2 節整合，並確保 pf 語法檢查成功。
常駐 /etc/vm.conf 不宣告 CI VM；runtime 定義放 /home/vmm-ci/job.vm.conf。
腳本建立真實衍生磁碟後，才驗證並 vmctl load 載入 runtime 定義。
工作停止後先 reload 常駐設定，移除已停止定義，再刪除磁碟。
VM 使用已保留的 tap8／ci group。

已修正兩個生命週期問題：提前參照 job.qcow2 會使 vmd -n 失敗；無磁碟
template 雖通過語法檢查，仍會使 vmd 在註冊時退出。因此採用上述動態載入，
不建立假磁碟。防火牆已完成者只需重跑 resume-vmd.yml。

## 3. 建置 guest 基底

**優先使用 [cloud-init 建置說明](../docs/CLOUD_INIT.md)**：

```sh
ansible-playbook playbooks/cloud-build.yml --ask-become-pass
```

它會建立 NoCloud seed、自動設定 guest 並安裝 runner，取代以下手動 ISO 路徑。

### 若 vmd 啟動後 vmctl 回報 Connection refused

防火牆已成功套用、SSH 重連及 rollback disarm 都完成時，不需重新套用防火牆。
執行只處理服務的恢復流程：

```sh
ansible-playbook playbooks/resume-vmd.yml --ask-become-pass
```

這會先驗證 vm.conf，必要時啟動 vmd，再用真正的 vmctl status 重試確認
控制介面就緒（不只檢查 socket 檔案存在）。成功才 reload 常駐設定。
此恢復流程亦會替換舊 managed template、更新 lifecycle script 與 runtime 定義。
失敗會顯示 rcctl 狀態／設定、socket、vmd process 與相關系統日誌。
不會盲目刪 socket、重啟既有服務或重跑防火牆。尚不能只憑 Connection refused
區分啟動時序或 daemon 退出，需以此輸出確認原因。

沒有任何 VM 時，OpenBSD 8.0 的 vmctl status 可只輸出表頭並回傳 1；
目前檢查會接受這種空清單（必須 stderr 空白且符合完整表頭），不再誤判成
vmd 故障。Connection refused、未知輸出或其他錯誤仍會失敗。

Ubuntu 安裝／serial console 依根目錄 README 第 3 節進行。這版不自動操作
ISO 安裝器，也不假設任意 Ubuntu cloud image 都能在 vmm BIOS 啟動。

建立可信的臨時 Linux admin 與 SSH key，從 host 的 vmctl status 取得 build VM ID，
local interface 的 guest IP 通常為 100.64.N.3。從 host 連入 guest 的 SSH 是管理
連線，與 guest 主動連回 host 的封鎖方向不同；仍需確認原有 pf 規則允許。

在 inventory/hosts.yml 的 linux_builders 加入 `ci_build: {}`，實際 guest IP、
帳號與 ProxyJump 放入 `sops edit inventory/host_vars/ci_build.sops.yml` 建立的
加密 host vars；不要將實際值填入 hosts.yml 的示例註解。保留不轉送 agent，
並核對 guest SSH host fingerprint。
這個 inventory 只用於乾淨的 build VM，不可拿來管理已跑過不可信 job 的 guest。

GitHub repo 設定的 New self-hosted runner 選 Linux / x64，取得版本與官方 SHA256，
用 `sops edit inventory/group_vars/all.sops.yml` 修改 ci_runner_version／ci_runner_sha256。

```sh
../.venv/bin/ansible-playbook playbooks/guest.yml --syntax-check
../.venv/bin/ansible-playbook playbooks/guest.yml --check --diff --ask-become-pass
../.venv/bin/ansible-playbook playbooks/guest.yml --ask-become-pass
```

這裡 --ask-become-pass 輸入的是 **Linux guest 的 sudo 密碼**，不是 host doas 密碼。
guest 需預裝 Python 3。get_url 的 check mode 不會驗證實際下載內容，第一次
guest --check 可能在 unarchive 因尚無 archive 而停止；完整下載校驗要在正常
執行完成。實際變更不包含 runner 註冊、服務啟動、關機或建置管理員移除。

guest playbook 安裝工具、原生依賴、無 sudo 的 gha-runner、指定版本 runner。
不自動 apt upgrade 全機；封存前由管理員更新 OS。跑過 job 或已註冊的 guest
會被拒絕當作基底。完成後移除臨時 build admin 的 SSH key／sudo 權限、
鎖定密碼；若需要 console 註冊，另設可互動登入、無 sudo 的 gha-runner 密碼。
確認無任何 host 管理 key、GitHub token、舊工作資料後關機並封存 github.qcow2。

## 4. 一次性 VM 的註冊與驗收

啟用 host 網路與 VM 設定後：

```sh
ssh -t ci-admin@192.0.2.10 'doas /usr/local/sbin/ci-run-vm github'
# 另一個 terminal
ssh -t ci-admin@192.0.2.10 'doas vmctl console ci-runner'
```

以無 sudo 的 gha-runner 登入 guest console：

```sh
cd ~/actions-runner
umask 077
./config.sh --url https://github.com/OWNER/REPO \
  --name homelab-vmm --labels openbsd-vmm --ephemeral
./run.sh
```

registration token 在 config.sh 互動提示輸入，不寫進 inventory 或 shell 命令。
測試 examples/smoke.yml，完成後由 host 停止 VM。host 腳本會刪除磁碟，
最長 60 分鐘（包括排隊）自動回收。驗收網路封鎖及 marker 不殘留後再接正式工作。

## 驗證狀態

38 項本機測試與 12 份 playbook 的 ansible-playbook --syntax-check 通過。
使用者已在主機完成網路啟用、基底與首個自動派工 job；SOPS 遷移另已完成
本機解密、inventory 與模板比對。跨重啟及故障情境仍需實機驗收，
詳見 [驗證記錄](../docs/VALIDATION.md)。

參考：[Ansible BSD 管理](https://docs.ansible.com/projects/ansible-core/2.21/os_guide/intro_bsd.html)、
[doas plugin](https://docs.ansible.com/projects/ansible/latest/collections/community/general/doas_become.html)。
