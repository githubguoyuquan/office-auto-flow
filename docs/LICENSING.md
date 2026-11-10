# AIOA 授权交付与构建

## 管理员签发工具

Mac 编译版：打开签发工具目录，运行 `启动签发工具.command`。
Windows 编译版：运行独立管理员目录中的 `AIOA-Issuer.exe`。
开发环境：安装 `packaging/requirements-build.txt` 后执行 `python license_issuer.py`。
浏览器进入本机 `http://127.0.0.1:54420/`，关闭工具的终端窗口即可退出。

首次使用时，设置私钥保护密码并下载密钥备份 ZIP。备份中：

- `private-key.pem` 是加密私钥，仅管理员保管。另行备份密码，丢失无法恢复。
- `public-keys.json` 是工作台编译所需的公钥，可交给构建电脑。

已有密钥后不要重复创建。不同密钥签发的授权，需要客户工作台嵌入相应公钥才能验证。
管理员工具、私钥备份和密码都不得交给客户；签发工具不在服务器磁盘留存私钥。

## 首次交付

1. 管理员创建密钥，把 **public-keys.json** 交给 Windows 构建流程。
2. 构建流程将公钥嵌入编译后的客户工作台，生成 Windows 安装程序。
3. 客户安装，在右上角“授权管理”中导出设备申请 JSON，交给管理员。
4. 管理员在签发台选择申请文件、加密私钥，填写保护密码、客户名称及有效期。
   有效期可按签发后的天数填写（支持小数），或选择本机时区下的明确到期时间。
5. 下载 `.license` 文件交给客户；客户导入后完成联网时间校验即可执行。
6. 续期时，用原签发密钥签发新的到期时间，客户导入新文件。更换电脑需新设备申请。

## 校验规则

- 使用 Ed25519 数字签名。客户只持有公钥，不持有签发私钥。
- 授权绑定系统设备标识，重装操作系统可能需要重新授权。
- 导入时、每次工作台进程启动后的首次执行，以及间隔达到 24 小时时联网校时。
- HTTPS 时间源为阿里云、Microsoft、Cloudflare 官网，至少两个来源时间一致才接受。
  请求仅带随机防缓存参数，不带授权、设备、客户或 OA 数据。
- 默认保持 TLS 证书校验；校时失败不延长旧时间有效期，禁止新任务，可手动重试。
- 运行中由页面自动尝试每日校验；服务端在每个新任务启动前独立验证。
- 到期不打断已经接受的整批任务。未授权、到期或设备不符时仍可打开界面、导入新授权和退出。
- 离线签名授权不支持远程撤销。公共网站提供的 HTTPS Date 不是专用的签名时间服务；
  依赖网站可用性及操作系统的 TLS 信任环境，不能防住被全面控制的操作系统或已修改的程序。

## Windows 本机构建

构建电脑需 Python 3.12 x64、Visual Studio 2022 C++ 构建工具（建议英文组件）、NSIS 3 和 Edge。
客户运行电脑只需 Windows 10/11 x64 和 Edge，不需要 Python。

在项目目录的 PowerShell 执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\build_licensed_windows.ps1
```

选择 public-keys.json 后，会安装构建依赖、运行测试、使用 Nuitka 编译、测试编译程序和浏览器、生成安装包。
添加 `-BuildIssuer` 可额外生成管理员专用的 Windows 签发工具目录。
成功后，`preview/Office-Auto-Flow-API-Licensed-时间-win-x64.exe` 是客户安装文件。

## GitHub Windows 构建

将代码推送至仓库后，在 Actions 中手动运行 `Build licensed Windows workbench`，输入 public-keys.json 的内容。
不得输入或上传私钥。下载 `AIOA-Customer-Installer` 交给客户；`AIOA-Owner-Issuer` 只交给管理员。
工作流只有仓库读取权限，不会自动发布版本或上传签发私钥。

GitHub 手动触发要求工作流已存在于默认分支。首次仅在开发分支构建时，可把公开的
公钥 JSON 保存为 `.github/license-public-keys.json` 并提交，然后向该提交推送
`licensed-build-版本` 标签触发构建，无需合并到 master。

## 编译保护

客户发行版原生编译授权模块和业务代码，包内不交付这些模块的 `.py` 源码。
构建会校验公钥、编译清单和安装程序输入，并拒绝包含应用源码、签发模块或私钥的客户包。
升级安装会移除安装目录中旧版入口源码及嵌入式 runtime，授权与客户配置保留在用户数据目录。
编译增加修改成本，并不承诺绝对防破解。
