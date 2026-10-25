# Office Auto Flow

本地运行的 OA 浏览器自动化工作台。当前版本提供业务入口管理、登录协助和页面内容读取；单号查询与列表操作将在后续迭代中加入。

## Windows 启动

1. 安装 Python 3，并启用 `Add Python to PATH`。
2. 将项目文件放在同一文件夹。
3. 双击 `start_oa_login.bat`。

首次启动会在 `%LOCALAPPDATA%\oa-login-cli\venv` 创建独立 Python 环境并安装 Playwright。随后工作台会在浏览器中打开。关闭启动用的命令窗口即可退出本地服务。

## 当前功能

- 保存、搜索、编辑和删除业务名称及页面地址。
- 打开业务页面并检测常见的用户名和密码登录表单。
- 在工作台提交登录信息，或在自动打开的 OA 浏览器中手动完成登录及二次验证。
- 登录后返回原业务页面，并显示页面标题、地址和正文（包含可读取的 iframe 内容）。
- 登录凭据仅用于当前运行，不保存到业务配置。

业务配置保存在 `%APPDATA%\oa-login-cli\operations.json`，浏览器会话保存在 `%APPDATA%\oa-login-cli\browser-profile`。

## 分支与版本

- `main`：稳定版本基线，只接收经过验证的发布版本。
- `develop`：日常集成开发分支。
- `feature/<name>`：较大的新功能从 `develop` 创建，完成后合回 `develop`。
- 发布标签使用 `v主版本.次版本.修订号`；测试阶段使用 `-alpha.N`，候选发布使用 `-rc.N`。

当前工作台为早期测试版本，标签为 `v0.1.0-alpha.1`。

## 文件

- `oa_login_cli.py`：本地 Web 服务、界面和 Playwright 自动化入口。
- `start_oa_login.ps1`：创建 Python 环境、安装 Playwright 并启动应用。
- `start_oa_login.bat`：双击启动 PowerShell 脚本。
