# 离线更新与签发

## 当前范围

- 更新包为签名的 `.oaf-update` 文件，包含完整界面资源快照，可选 Windows x64 PPTX 提取组件。
- 界面调整无需重新编译客户程序；PPTX 内核调整只需重新编译 Go 组件并签发更新包。
- OA Python 后端、Python/浏览器运行环境、更新协议或验签公钥变更，当前仍需完整安装包。
- 发布工具只供管理员使用，不进入客户安装包。客户数据、许可证和登录会话独立保存。

## 1. 首次创建独立更新密钥

在项目 Python 环境运行 `python update_publisher.py serve`，打开 `http://127.0.0.1:54423/`。

在“首次创建更新密钥”设置保护密码，下载并解压备份：

- `update-private-key.pem`：加密私钥，仅管理员保管。
- `update-public-keys.json`：公开验签公钥，用于编译客户基础安装包，可以提交给构建服务。

请另存密钥备份和密码。已有更新密钥时直接签发，避免重新创建导致旧客户无法验证。
更新密钥必须与软件许可证密钥不同；构建程序会拒绝复用。
网页不会把私钥或密码写入服务器文件或发布记录。

命令行也可创建，密码通过交互输入，不放入命令参数：

```sh
python update_publisher.py keygen --output /path/to/new-update-key-backup
```

## 2. 构建一次支持更新的基础安装包

GitHub Windows 构建现在需要两个公开参数：

- `public_keys_json`：原软件许可证公钥 JSON。
- `update_public_keys_json`：新创建的更新发布公钥 JSON。

标签构建分别读取 `.github/license-public-keys.json` 和 `.github/update-public-keys.json`。
仓库中只能保存公钥，不能保存私钥、密码或密钥备份 ZIP。

Windows 手工构建：

```powershell
./packaging/build_licensed_windows.ps1 -PublicKeys ./public-keys.json -UpdatePublicKeys ./update-public-keys.json
```

基础程序会嵌入独立更新公钥、更新协议版本和基础版本序号；源码开发版默认未启用正式更新。
客户先退出旧工作台，再覆盖安装本基础版本。之后界面和 PPTX 内核的小更新无需完整安装包。

## 3. 签发小型更新包

在本地签发页面选择加密更新私钥，填写密码、版本名称和界面资源目录，点击签发下载。
界面资源目录默认使用当前项目的 `web`。网页下载文件不覆盖原文件。

发布序号留空时使用 UTC 时间序号，并依据发布记录递增。版本名称用于显示，序号用于排序。
发布记录保存在 `.secrets/update-publisher/release-ledger.json`，应随管理工具配置备份。
同一密钥建议由一个签发端管理；换电脑时迁移发布记录。下载中断可重新签发，序号出现间隔正常。
手填序号必须大于上次发布和客户基础程序的序号。请在基础程序构建后再签发适配它的更新包。

命令行示例：

```sh
python update_publisher.py build --version 0.3.1 --private-key /path/to/update-private-key.pem --output /path/to/Office-Auto-Flow-0.3.1.oaf-update
```

PPTX 内核更新先构建并验证 Windows x64 Go 程序，然后通过页面的组件完整路径或命令行 `--component` 加入。
未加入组件时，该更新快照使用基础安装包的提取组件；不会继承其他更新快照中的组件。
签发时校验组件架构，客户安装时检查界面脚本并实际运行组件的模板读取自检。

## 4. 客户导入与回退

任务结束后进入“应用配置 → 软件更新 → 导入更新包”。
客户端校验签名、兼容性、路径、文件大小及哈希，暂存并自检成功后原子切换版本，自动刷新界面。
刷新会清空尚未执行的文件选择，已保存配置和输出文件保留。
更新失败会保留当前版本。更新期间不能开始任务，任务期间不能更新。

可点击“回退上一版本”。如果更新界面无法使用，打开固定恢复地址：

`http://127.0.0.1:54418/updates/recovery`

恢复地址始终使用基础界面。损坏的更新资源不会被加载。回退保留最高已安装序号，避免重新导入旧包。
覆盖安装更高版本的完整程序后，低于该基础版本序号的更新会被忽略，避免旧界面覆盖新程序。

当前不提供联网下载或远程强制更新。
