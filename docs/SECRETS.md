# Git 與 SOPS 設定

實際主機連線與部署變數已用 SOPS 加密，Git 只保存密文：

- `ansible/inventory/hosts.yml`：主機別名、群組與非敏感的連線設定。
- `ansible/inventory/host_vars/homelab.sops.yml`：IP 與 SSH 帳號。
- `ansible/inventory/group_vars/all.sops.yml`：repo、App ID、私鑰路徑與版本釘選。
- `.sops.yaml`：加密檔案規則與公開 recipient。

目前 recipient 是本機既有 `~/.ssh/id_ed25519.pub` 的公鑰，不含公鑰註解。
SOPS 3.10 以上可透過 age 使用 SSH Ed25519 recipient；本機已用 SOPS 3.13.3
完成加密、解密與資料比對。解密使用原有 `~/.ssh/id_ed25519`，沒有複製或
產生新的解密私鑰。GitHub App PEM 仍留在原本的專案外路徑，未加入 Git。

Ansible 使用 `community.sops.sops` vars plugin，自動在控制端記憶體解密，
不需輸出明文 inventory。安裝 collection 後，原本的 playbook 命令不變：

```sh
cd ansible
ansible-galaxy collection install -r requirements.yml
ansible-playbook playbooks/preflight.yml --ask-become-pass
```

若解密私鑰不在預設位置，設定檔案路徑；不要把私鑰內容放進命令：

```sh
export SOPS_AGE_SSH_PRIVATE_KEY_FILE=/absolute/path/to/private-key
```

## 修改與備份

從專案根目錄編輯，SOPS 儲存時會重新加密：

```sh
sops edit ansible/inventory/host_vars/homelab.sops.yml
sops edit ansible/inventory/group_vars/all.sops.yml
```

SOPS 編輯會讓 editor 接觸明文，請停用該 editor 對敏感檔的 swap、自動備份
與非本機同步。不要將 `sops decrypt` 或 `ansible-inventory --list` 的明文輸出
另存進 repo、貼入 issue 或 CI log。

遷移前的 inventory 備份保存在本機 `.local/backups/inventory/`，目錄 0700、
檔案 0600，已由 `.gitignore` 排除。備份含明文，確認密文可用後可自行刪除。
文件內 `192.0.2.10`、`ci-admin` 是示例；實際值以解密後的 inventory 為準。

換電腦時，需要經其他安全管道取得解密私鑰及原本的 App PEM；clone repo
本身不會提供這兩者。新增 recipient 後，以 `sops updatekeys <檔案>` 更新
兩份密文。請在移除舊 recipient 前，先驗證新身分確實可解密。

## 提交前檢查

`.gitignore` 排除 env、私鑰、明文 inventory、VM 映像、cloud-init seed、
執行目錄與本機備份。首次 clone 後可啟用 repo 提供的 pre-commit hook：

```sh
git config --local core.hooksPath .githooks
python3 scripts/check-secrets.py
python3 scripts/check-secrets.py --history
```

檢查器掃描 Git index 中的實際 blobs，包含 `git add -f` 加入的檔案；
`--history` 另查所有已提交的檔案版本。只輸出路徑與問題種類，不列出命中值。
它會阻止已知私鑰／token 格式、禁止的檔名、明文私有主機 IP，以及未加密的
inventory 變數。防火牆使用的 CIDR 網段與文件範例位址可以提交。
SOPS 檔名檢查只辨識密文結構；真正的解密驗證另外使用 SOPS 檢查 MAC 並比對
原始資料。這是針對本專案的規則，不是所有憑證格式的完整偵測器。

每個 commit 使用 Conventional Commits，按設定保護、VM／網路、基底映像、
JIT 控制器與操作文件分開，相關測試與功能一起提交。

來源：[SOPS age／SSH identities](https://github.com/getsops/docs/blob/main/content/en/docs/usage/identities/age/_index.md)、
[Ansible SOPS vars plugin](https://docs.ansible.com/projects/ansible/latest/collections/community/sops/sops_vars.html)。
