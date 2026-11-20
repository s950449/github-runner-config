# Forgejo runner：獨立的一次性 Linux VM

共用 OpenBSD host 與一次一個 job 的容量鎖，不共用 GitHub 的已註冊 guest。
Forgejo server 不放在會銷毀的 runner VM；runner 不持有 server 管理權限。

## 執行模式

本配置採用 `host` executor，但這裡的 host 是**一次性的 Linux guest**，
不是 OpenBSD。如此不用把 rootful Docker socket 開給 job。
runner 與 job 使用同一個無 sudo 帳號；要假設 job 能讀取 runner 設定／憑證。
所以一定使用 ephemeral 身分，每個 VM 只執行一個 job，接著銷毀。

先用安裝的 binary 檢查：

```sh
forgejo-runner --version
forgejo-runner register --help
forgejo-runner one-job --help
forgejo-runner generate-config --help
```

最新文件已將舊 `register` 流程標示 deprecated；server／runner 版本未確認前，
不要混用不同世代的 config.yml。從該版本 generate-config 產生設定，再設定
容量為 1、host label 為 `openbsd-vmm:host`，不配置 Docker socket、privileged
容器、host 共享目錄或額外 server connections。

## 舊 register CLI 仍受支援時的手動流程

1. 在 Forgejo 的目標 repo 設定取得 runner registration token，使用 repo scope。
2. host 執行 `doas /usr/local/sbin/ci-run-vm forgejo`。
3. 在 guest console 以 gha-runner 登入，建立私人工作目錄、umask 077。
4. 執行 `forgejo-runner register --ephemeral`，互動輸入 HTTPS instance URL、
   token、唯一 runner 名稱；label 使用 `openbsd-vmm:host`。
   若該版本改由設定檔宣告 label，依該版本的 generate-config 設定。
5. 使用 `forgejo-runner one-job` 執行，設定檔參數以安裝版本的 help 為準。
   ephemeral runner 不使用 daemon。
6. 把 examples/forgejo-smoke.yml 放入 repo 的 .forgejo/workflows/smoke.yml，
   手動執行。完成後由 host 停止 VM，回收腳本刪除磁碟。

如果 runner 已移除 register CLI，改走官方 server-side registration 流程：
在 server 或可信控制層建立 repo-scoped ephemeral runner，將該次 UUID／secret
放入一次性 guest 設定。不要把 server 管理 token、registration token 或既有
runner secret 烘焙進基底。具體命令等待你的 server／runner 版本後補齊。

Forgejo 自動 workflow token 的權限行為要按 server 版本驗證；不能因為
GitHub 的 `permissions: {}` 有效，就假設 Forgejo 也有相同語意。
第一個 smoke workflow 不 checkout、不注入 secrets。後續先驗證 token 的
實際寫入能力，再接 agent 工作；此設定不保證 workflow token 是唯讀。

## 網路

Forgejo 在公網：HTTPS 443 可通時不需開 LAN。
Forgejo 在家中：只允許該固定 IP 的 HTTPS port，規則必須在 host／私網封鎖之前。
使用有效 TLS 憑證；自簽 CA 應明確安裝信任，不使用 skip verification。
action 原始碼／套件可能從其他網域下載，按實際依賴補連線政策。
同一 IP:port 同時提供管理 API 時，pf 無法區分 API 路徑，仍靠平台認證保護。

## 參考

- https://forgejo.org/docs/latest/admin/actions/security/
- https://forgejo.org/docs/latest/admin/actions/registration/
- https://forgejo.org/docs/latest/user/actions/security/
