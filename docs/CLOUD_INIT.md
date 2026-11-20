# Cloud-init 建置 GitHub runner 基底（建議流程）

取代手動 ISO 安裝；目標仍為 OpenBSD 8.0、2 vCPU、8GB RAM、30GB 磁碟。
在專案的 ansible 目錄執行。host 已完成先前網路／vmd 設定才可開始。

## 1. 建置

```sh
ansible-playbook playbooks/cloud-build.yml --ask-become-pass
```

預設使用控制端 ~/.ssh/id_ed25519.pub。若使用其他公鑰：

```sh
ansible-playbook playbooks/cloud-build.yml --ask-become-pass \
  -e ci_builder_public_key_file=/absolute/path/to/your-key.pub
```

只讀公鑰，私鑰不會傳到 OpenBSD 或 guest。playbook 會安裝 OpenBSD xorriso、
下載固定日期的 Ubuntu qcow2、驗證釘選 SHA256，建立 cidata ISO，再啟動 ci-build。
輸出會包含 guest IP 與 SSH／console 命令。過程不會改防火牆、不註冊 GitHub runner。

cloud-init 會建立：

- ci-builder：只供基底建置的 SSH 管理帳號，暫時有 NOPASSWD sudo。
- gha-runner：鎖定密碼、沒有 sudo 的正式工作帳號。
- 依 MAC 匹配的 DHCP 網路、指定 DNS、停用 guest IPv6、自動擴展根分割區。
- serial console、必要套件、固定版本及 SHA256 校驗過的 runner binary。
- /usr/local/sbin/ci-seal-image，供完成驗收後撤銷建置管理權限並關機。

基底不含 GitHub PAT、registration token 或 runner 註冊身分。
腳本不覆寫既有 build.qcow2；失敗後先檢查原因，不要盲目刪磁碟重建。
建置期間持有 runtime/lock 與 build.lock，會阻擋其他測試工作。
若在建立磁碟之前失敗，可檢查沒有 VM 執行後刪除這兩個鎖再重跑；
若已建立磁碟，先檢查／恢復 ci-build，不在不明狀態下重跑建置。

### compressed clusters unsupported 的恢復

Ubuntu 原始 qcow2 使用壓縮 cluster，OpenBSD vmctl 無法直接 copy/convert。
已改為安裝 OpenBSD qemu package 的 qemu-img，先解碼成 sparse RAW，
比對原始資料一致後再由 vmctl 從 `raw:` 輸入建立自己的 30GB qcow2，
檢查 metadata 與再次比對資料後才發佈 build.qcow2。
不使用 QEMU 產生的 compat=0.10 qcow2：該版本 header 的 refcount 欄位也會
觸發 OpenBSD 讀取器的 unsupported refcount size。RAW 沒有這些 metadata。
QEMU 只用作離線磁碟工具，不啟動另一個 hypervisor 或常駐服務。

若先前流程停在這個轉換錯誤，執行：

```sh
ansible-playbook playbooks/resume-cloud-build.yml --ask-become-pass
```

不需刪 lock、重下載來源或重新建 seed。此恢復流程限定在「build lock 與 seed
存在、沒有任何 VM 執行、build.vm.conf 尚未建立」的失敗階段；再次校驗來源
SHA256，既有部分 build.qcow2 會移到 base/failed-build-* 保留，絕不直接覆寫。
新轉換在私有暫存目錄完成、驗證成功後才搬到 build.qcow2，然後繼續啟動 ci-build。
如果已經啟動過 guest 或載入過 build.vm.conf，此恢復流程會拒絕，避免誤改用過的磁碟。

格式選項依 [QEMU 磁碟文件](https://www.qemu.org/docs/master/system/images.html)
與 [qemu-img 文件](https://www.qemu.org/docs/master/tools/qemu-img.html)；不使用 -c。

### 已完成轉換，但 Start build VM 失敗

不要再跑磁碟轉換的恢復流程；此時 build.vm.conf 已建立，磁碟必須保留。

```sh
ansible-playbook playbooks/resume-build-vm.yml --ask-become-pass
```

此流程列出 /dev/vmm、/dev/tap8、BIOS、build disk、seed ISO 的實際狀態。
若只有 /dev/tap8 缺少，使用 OpenBSD MAKEDEV 建立；不修改既有裝置權限。
`ifconfig tap8 create` 建立網路介面，不保證已存在對應的 character device。
確認開機資源存在後才載入並啟動 VM；失敗則列出真正的 vmd 系統日誌。
不改防火牆、不重下載／轉換磁碟、不重新產生 seed；也不重新啟動 vmd。
固定介面的 device 需求參考 [OpenBSD tun/tap 文件](https://man.openbsd.org/tap.4)。

## 2. 驗證 cloud-init

```sh
# 先從 host 的 serial console 核對 guest 輸出的 SSH host fingerprint。
ssh -t ci-admin@192.0.2.10 'doas vmctl console ci-build'
# 然後用 playbook 顯示的 IP；私鑰若非預設，額外加 -i。
ssh -J ci-admin@192.0.2.10 ci-builder@100.64.N.3
```

不要關閉 SSH host key checking。guest 內執行：

```sh
sudo cloud-init status --wait
sudo test -f /var/lib/ci-image-ready && echo READY
sudo -iu gha-runner id
sudo -iu gha-runner sudo -n true  # 應失敗
```

cloud-init 如回報 error／degraded 或沒有 READY，查看
`sudo tail -n 100 /var/log/cloud-init-output.log`，此時不要封存。

### 修復 groups 空陣列造成的 degraded

若 native schema 明確回報 users.1.groups 的 [] 不合法，已提供現有磁碟修復：

```sh
# 在控制端 ansible 目錄執行，不是在 Ubuntu guest。
ansible-playbook playbooks/repair-cloud-init.yml --ask-become-pass
```

此流程從 host 的 vmctl 狀態找出 ci-build IP，透過既有已信任的 SSH host key
及 ci-builder 公鑰連線。先在 guest 用 cloud-init schema 驗證新 user-data，
確認沒有 runner 註冊或工作資料，備份原始 schema／logs，再移除舊 READY、
clean cloud-init instance state 並正常關機。host 在確認停止後才替換 seed ISO，
使用相同磁碟重開；不重新下載 Ubuntu、不轉換或重建磁碟、不改防火牆。
新 seed 在此次修復保留現有 SSH host keys；正式封存時仍會刪除 host keys。

啟動後使用新的輸出 IP，重新檢查 cloud-init status --long、schema --system，
以及 READY marker。舊 recoverable error 透過真正重新執行初始化來清除，
不編輯 status.json／result.json，也不以忽略 exit code 當成成功。
若 SSH host key 或公鑰無法驗證，會停止；不要關閉 host key checking。

若 DNS／連網失敗，先確認 seed 是否被讀取，以及 host pf 的規則計數。
runner package 的進度式發布可能因 repo 不同而有差異；必要時依 GitHub repo
New self-hosted runner 頁面調整版本及雜湊，不能只改版本號。

## 3. 封存

確認 cloud-init、工具與網路隔離驗收都通過，guest 內執行：

```sh
sudo /usr/local/sbin/ci-seal-image
```

腳本會確認尚未註冊／執行 job、撤銷 ci-builder 的公鑰和 sudo 規則、清理
SSH host keys 與 cloud-init seed／instance state／machine-id，然後關機。
SSH 中斷是關機的預期結果；如指令提早報錯，先處理，不可直接封存。
腳本不刪除完整 ci-builder 帳號，但帳號密碼鎖定、沒有 authorized_keys 或 sudo。

guest 關機後，在控制端執行：

```sh
ansible-playbook playbooks/finalize-image.yml --ask-become-pass -e ci_guest_sealed=true
```

ci_guest_sealed 表示你已成功執行 guest 封存腳本；host 不能只靠「沒有 VM 執行」
證明 guest 內容已清理。playbook 會要求沒有任何執行中的 VM、既有基底不存在，
先 reload 移除停止的 build 定義，再將磁碟搬成 base/github.qcow2、設為 0400，
移除建置 seed 與容量鎖。

## 4. 每個 job 的 cloud-init

```sh
ssh -t ci-admin@192.0.2.10 'doas /usr/local/sbin/ci-run-vm github'
```

host 會建立新的磁碟及唯一 instance-id 的 seed ISO。這個 seed 只設定
gha-runner 的 SSH 公鑰及網路，**不建立 ci-builder、不授予 sudo、不含 token**。
guest 使用同一份公鑰但控制端私鑰不進 VM。請用 vmctl status 取得 ci-runner ID N，
核對新 guest 的 host fingerprint 後登入：

```sh
ssh -J ci-admin@192.0.2.10 gha-runner@100.64.N.3
cd ~/actions-runner
./config.sh --url https://github.com/OWNER/REPO \
  --name homelab-vmm --labels openbsd-vmm --ephemeral
./run.sh
```

token 只在 config.sh 提示中輸入。執行 smoke workflow 後由 host 停止 VM；
60 分鐘上限仍生效。回收先移除停止的 VM 定義，再刪磁碟與 seed ISO。
GitHub 管理憑證與自動派工控制器仍留待後續，不會烘焙到基底。

## 驗證與來源

Ubuntu image 釘選 20261017 build；官方 manifest 含 grub-pc／grub-pc-bin，
可作為 BIOS 啟動相容性的依據，但仍需在這台 vmm 驗證首次開機。
SHA256 來自官方 HTTPS 發布資料；本流程沒有另行執行 GPG 簽章驗證。
本地已完成 playbook 語法、cloud-config YAML／shell 與 lifecycle mock 測試；
尚未在你的 OpenBSD 下載或開機，不能視為實機建置已通過。

- [Ubuntu 固定日期映像](https://cloud-images.ubuntu.com/noble/20261017/)
- [官方映像 manifest](https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.manifest)
- [GitHub runner 2.338.2 與 SHA256](https://github.com/actions/runner/releases/tag/v2.338.2)
- [NoCloud seed 格式](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
