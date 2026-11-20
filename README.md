# OpenBSD 8.0 上的 GitHub／Forgejo 一次性測試 VM

基底與手動 runner 已完成實機驗證後，接續使用
[GitHub App 自動派工部署](docs/AUTOMATION.md)：60 秒輪詢、JIT 註冊、單 VM 執行與回收。

針對 i5-8250U、24GB RAM、/home 約 109GB 可用的單機 homelab。
Linux guest：Ubuntu Server 24.04 amd64，2 vCPU、8GB RAM、30GB 虛擬磁碟。
全機一次一個 job；GitHub 與 Forgejo 分別使用基底映像。

**目前部署範圍只考慮 GitHub `OWNER/REPO`。** 已新增
[Ansible 部署方式](ansible/README.md)，目標為 `ci-admin@192.0.2.10`，
以需要密碼的 doas 提權。Forgejo 檔案暫時保留，不由 playbook 部署。

實際 IP、SSH 帳號與 App 設定以 SOPS 密文納入版控；文件中的位址與帳號為
示例。解密、編輯與提交前檢查見 [Git 與 SOPS 設定](docs/SECRETS.md)。

**Ubuntu 基底現在建議使用 [cloud-init 全自動建置流程](docs/CLOUD_INIT.md)**，
以下手動 ISO 安裝步驟保留作為備用。cloud-init 會完成帳號、網路、runner binary
與封存準備，不需要互動式安裝 OS。

使用者已在 OpenBSD 主機完成基底、手動 runner 與首個 GitHub App 自動派工 job
驗收；host 日誌確認 VM 啟動與回收。自動模式每次建立一次性 runner，
沒有排隊工作時不維持在線 VM。跨重啟與故障情境的驗收狀態見
[驗證記錄](docs/VALIDATION.md)。

## 1. Host 準備（OpenBSD 管理員）

```sh
uname -r
dmesg | egrep '(VMX/EPT|SVM/RVI)'
doas syspatch
doas fw_update
doas rcctl enable vmd
doas rcctl start vmd
doas install -d -o root -g wheel -m 700 /home/vmm-ci
doas install -d -o root -g wheel -m 700 /home/vmm-ci/base /home/vmm-ci/runtime
```

所有映像放 /home，不放只有 986MB 的 / 或 28GB 的 /var。
30GB qcow2 是稀疏磁碟，但基底加上衍生磁碟仍可能接近 60GB。
至少保留 15GB host 空間；建立第二種 runner 的基底前重新估算容量。
此配置尚未對 /home 設定硬性配額；正式接受不可信工作前，應使用專用
檔案系統或設定並實測 root/vmd 實際寫入身分適用的配額。

## 2. 建立網路隔離

把 [host 配置](openbsd/vmd-host.conf) 的管理區塊合併到 /etc/vm.conf，
此區塊只有註解，不宣告 CI VM。不要把 runtime VM 定義合併進常駐設定。
把 [runtime VM 配置](openbsd/vm.conf.example) 安裝為
/home/vmm-ci/job.vm.conf。腳本先建立磁碟，再用 vmd -n 驗證此檔、
vmctl load 載入，最後啟動 ci-runner。回收時先 reload 常駐設定再刪磁碟。
VM 使用 tap8／ci group；啟動服務時不需要 job 磁碟，也不註冊無法開機的 VM。

把 [pf 篩選範本](openbsd/pf-ci.conf.example) 放到 /etc/pf-ci.conf。
閱讀並修改其中的內網／VPN／公網管理位址清單。在 /etc/pf.conf 的
filter 區域、任何廣泛的 pass quick 規則之前加入：

```pf
include "/etc/pf-ci.conf"
```

在主規則的 NAT/match 區域加入（保留原有規則）：

```pf
match out on egress inet from 100.64.0.0/10 to any nat-to (egress)
```

若其他 VM 或 VPN 使用 100.64.0.0/10，需縮小 NAT 來源並調整位址規劃。
不得對 tap8 或 ci 使用 set skip；不要橋接到家用 LAN，不啟用 local inet6。
範本明確拒絕 guest IPv6。不要在 guest 上做唯一一道隔離。

```sh
doas cp /etc/pf.conf /etc/pf.conf.before-ci
doas pfctl -nf /etc/pf.conf
# 保留本機 console，再套用；不要盲目透過遠端修改防火牆。
doas pfctl -f /etc/pf.conf
doas sysctl net.inet.ip.forwarding=1
```

驗證後將 `net.inet.ip.forwarding=1` 合併進 /etc/sysctl.conf。
若原有 state 已容許 guest 連入 LAN，單純 reload 不會撤銷它；先停止
guest，再以 pfctl 檢查並刪除該 guest 相關 state，避免全域 flush 中斷其他服務。
目前 HTTP(S) 可到任意公網：可以外洩原始碼。更嚴格的部署需另做 egress proxy。

Forgejo 若位於 LAN，先不要放寬整個網段。只在拒絕 host／內網的規則之前
允許「Forgejo 固定 IP:HTTPS port」，並驗證不能到 SSH、資料庫或管理 port。
若 Forgejo 就在 OpenBSD host 上，也只放行其 HTTPS listener。
精確例外需根據你的 Forgejo 位址補齊，預設不開。

## 3. 建立未註冊的 Linux 基底

從 Ubuntu 官方取得 24.04 Server amd64 安裝 ISO，驗證官方簽章與 SHA256SUMS。
存為 /home/vmm-ci/ubuntu.iso。使用 BIOS 開機與 serial console；安裝媒體的
GRUB kernel command line 加上 `console=ttyS0,115200n8`。若 console 看不到
GRUB，需預先製作 serial-enabled 安裝媒體；不要把「沒畫面」當作 VM 卡住。

```sh
doas vmctl create -s 30G /home/vmm-ci/base/build.qcow2
doas vmctl start -m 8G -p 2 -L -r /home/vmm-ci/ubuntu.iso \
  -d /home/vmm-ci/base/build.qcow2 ci-build
doas vmctl console ci-build
```

安裝期間也要使用受限的介面：上面的臨時 VM 會自動配置 tap，需用
`ifconfig` 找到描述中包含 ci-build 的介面，執行 `doas ifconfig tapN group ci`。
加入 group 前不要在它裡面執行任何 repo 工作。
guest 內手動指定 DNS 為 1.1.1.1：vmd DHCP 預設提供的 host DNS 被本範本阻擋。
可在 Ubuntu 的 netplan DHCP 網路設定加入：

```yaml
dhcp4-overrides:
  use-dns: false
nameservers:
  addresses: [1.1.1.1]
```

保留原來的介面名稱和 dhcp4: true，執行 `sudo netplan try` 確認。
guest 預設經 NAT 校時可能失敗（未放行 UDP 123）；可在完成映像前校準時間，
或另外只放行指定公網 NTP server 的 UDP 123，勿任意開放其他流量。

安裝時建臨時管理帳號；安裝完成設定 /etc/default/grub 的：

```sh
GRUB_TERMINAL=serial
GRUB_SERIAL_COMMAND="serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1"
GRUB_CMDLINE_LINUX="console=ttyS0,115200n8"
```

```sh
sudo update-grub
sudo systemctl enable serial-getty@ttyS0.service
sudo apt update
sudo apt upgrade -y
sudo apt install -y ca-certificates curl git tar nodejs
sudo adduser --disabled-password --gecos '' gha-runner
sudo chmod 700 /home/gha-runner
```

管理員預裝測試工具。不要將 gha-runner 加入 sudo、docker、lxd 群組。
停用不需要的 sshd，臨時管理帳號完成建置後刪除 sudo 群組資格並鎖密碼。
runner 帳號需要 console 互動登入時，可設定僅供此 VM 使用的密碼；這個帳號
沒有管理權限。不要把 host 或其他服務的密碼複用於 guest。

### GitHub 基底

repo Settings → Actions → Runners → New self-hosted runner → Linux / x64。
在 guest 以 gha-runner 下載頁面提供的 runner 包、核對 SHA256、解壓到
/home/gha-runner/actions-runner；缺少相依套件時由管理員執行
`bin/installdependencies.sh`。**此時不要執行 config.sh 註冊。**

關機，host 用 `vmctl status -r` 確認 ci-build 已停止，再執行：

```sh
doas mv /home/vmm-ci/base/build.qcow2 /home/vmm-ci/base/github.qcow2
doas chmod 400 /home/vmm-ci/base/github.qcow2
```

### Forgejo 基底

建另一份未註冊基底，安裝與 server 相容的官方 forgejo-runner linux-amd64
binary，驗證 release 提供的雜湊／簽章，安裝於 /usr/local/bin/forgejo-runner。
不要使用 GitHub runner binary。可共用乾淨的 OS 範本，但不能從跑過 job
或註冊過 runner 的磁碟建立基底。存成 base/forgejo.qcow2，權限 400。

Forgejo runner 版本、設定格式與註冊方式請見 [Forgejo 操作說明](docs/forgejo.md)。

## 4. 執行一個 GitHub job

先合併 vm.conf 後執行 `doas vmctl reload`。
將 run-vm.sh 安裝成 root 擁有、不可由一般帳號修改的檔案：

```sh
doas install -o root -g wheel -m 700 openbsd/run-vm.sh /usr/local/sbin/ci-run-vm
doas /usr/local/sbin/ci-run-vm github
```

腳本持有全域鎖、建立衍生磁碟、啟動 ci-runner，最多存活 60 分鐘。
另一個 terminal 執行 `doas vmctl console ci-runner`，在 guest 登入 gha-runner：

```sh
cd ~/actions-runner
umask 077
./config.sh --url https://github.com/OWNER/REPO \
  --name homelab-vmm --labels openbsd-vmm --ephemeral
# 在互動提示貼上 repo 註冊 token，不要將 PAT 放進 guest。
./run.sh
```

把 examples/smoke.yml 放進 repo 的 .github/workflows/smoke.yml，使用網頁
workflow_dispatch 手動執行。這個 smoke test 不 checkout，不需要 repo 讀取權限。
job 完成後 runner 退出；回到 host 執行 `doas vmctl stop -fw ci-runner`。
管理腳本會確認 VM 停止後刪除 job 磁碟。

ephemeral 只負責一個 job，**不會自動關機或銷毀磁碟**。
此版本仍需手動停止，逾時則由 host 腳本強制停止。
60 分鐘包含等候註冊、等候工作與執行時間，不要安排超過此期限的 job。

需要 checkout 時：job 給 `contents: read`、checkout 設定
`persist-credentials: false`，action 固定在核對過的完整 commit SHA。
不要使用不可信 PR 的內容修改派工腳本，不讓測試 runner 持有部署憑證。

## 5. 驗收與回收故障

- guest 能 HTTPS 連到 GitHub／指定 Forgejo，DNS 與 TLS 時間正確。
- guest 連不到 host SSH、路由器 UI、NAS、其他 VM；IPv6 也不能繞過。
  由管理員選擇已知 IP/port，以 `nc -vz -w 3 IP PORT` 測試，並觀察 pf 計數。
- `id` 不是 root，`sudo -n true` 失敗，無 Docker socket／管理 key。
- 在一次性 guest 建 marker，回收後重建確認 marker 不存在。
- 在測試前用縮短的 host deadline 演練逾時：VM 被強制停止，磁碟清除。
- 停止 guest 後主機仍可運作、磁碟空間足夠，報告已存到平台。

host 重開機／腳本被 SIGKILL 時 EXIT trap 不會執行。開機後不要直接刪 lock：
先 `doas vmctl status -r`，如仍在跑則 `doas vmctl stop -fw ci-runner`；
**確認已停止**後，刪除 /home/vmm-ci/runtime/job.qcow2 與 runtime/lock。
腳本遇到無法查詢狀態或無法停止會保留磁碟／鎖，需人工處理。
GitHub／Forgejo 上未領到 job 的離線註冊也應手動刪除。

基底更新只在乾淨維護 VM 進行；更新 OS、runner 與依賴，驗證後替換已停止的
基底。不要將 job 磁碟的修改合併回基底。每次更新後重新驗收。

## 6. 後續自動派工

本版先不保存平台管理憑證。完整自動化接下來要增加：

1. 控制層讀取 GitHub／Forgejo 佇列，只接受預先允許的 repo。
2. 共用單一容量鎖選擇平台，啟動對應基底；不接受 job 自訂磁碟路徑或 shell。
3. 平台管理憑證留在控制層；只把該次 runner 身分傳到 guest。
4. 自動收集結束狀態、停止 VM、刪除磁碟與閒置註冊；處理重開機與失敗重試。
5. 不把 guest 回傳的測試報告當成 host 命令執行。

Forgejo server／runner 版本、URL，以及兩平台 repo 名稱確認前，不硬編碼
API 或憑證傳遞方式。此階段不應宣稱已完成無人值守的 software factory。

## 官方參考

- [OpenBSD virtualization](https://www.openbsd.org/faq/faq16.html)
- [vm.conf](https://man.openbsd.org/OpenBSD-8.0/vm.conf.5)
- [vmctl](https://man.openbsd.org/OpenBSD-8.0/vmctl.8)
- [pf.conf](https://man.openbsd.org/OpenBSD-8.0/pf.conf.5)
- [GitHub runner registration](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
- [GitHub ephemeral runner](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
- [Forgejo runner security](https://forgejo.org/docs/latest/admin/actions/security/)
