# 公共主线与客户分支

## 分支职责

| 分支 | 职责 | 合并来源 |
| --- | --- | --- |
| main | 公共 API 工作台的稳定基线 | develop |
| develop | 公共功能开发和集成 | feature/*、fix/* |
| customer/vgrass | VGRASS 客户交付版本 | main、客户专属修复 |
| customer/<客户代号> | 其他客户的专属版本 | main、对应客户修复 |
| feature/browser-automation | 原浏览器驱动方案，独立保留 | 仅明确指定的浏览器方案工作 |
| feature/api-driven-process-sheet | 改造前 API 分支，保留历史 | 后续公共开发迁至 develop |

默认开发目标为 API 公共主线；涉及客户专属字段时才切换客户分支。
2026-11-13 改造前的 main、develop、API 提交均有 archive/pre-customer-*-20261113 标签。
原 develop 的浏览器提交保存在浏览器分支及备份标签中；公共主线采用 API 实现。

## 客户差异

公共协议默认值位于 customer_profile.py，公共主线不跟踪 customer-profile.json。
客户分支独立提交 customer-profile.json，主要配置：

- customer_id、固定 business_url；公共主线按用户配置的登录站点推导 business_path。
- query_endpoint、detail_endpoint、api_base_path、query_defaults。
- fields：查询款号、结果款号、唯一编号、次数、明细参数、正式款号和模板字段。
- compatibility_hosts：允许应用旧 OA 脚本兼容补丁的站点。

业务地址与映射不提供工作台编辑入口。客户配置随分支交付，编译包记录其 SHA256，安装包构建核对配置一致性。
签发公钥沿用当前正式公钥；客户字段配置不改变产品授权标识或设备授权协议。

目前默认适配器仍遵循现有 OA 的分页、明细、打印页和表格结构；更换完全不同的 OA 系统需要新增适配器，不能只改字段名。

## 公共更新同步

1. 从 develop 创建 feature/<功能> 或 fix/<问题>，开发并验证后合回 develop。
2. 公共版本完成验证后将 develop 合入 main。
3. 对每个客户分支合并 main，检查配置和业务回归后分别发布；不要反向合并整个客户分支。

```sh
git switch customer/vgrass
git merge main
python -m unittest discover -s tests -v
```

公共主线不新增同名客户配置文件，正常合并不会覆盖客户文件。
若公共配置结构发生变化，必须同时提供迁移说明，并在每个客户分支补齐新字段；发生冲突应逐项处理，禁止整批采用某一侧覆盖。
客户修复中属于公共缺陷的部分单独提取至 fix/*，验证后再从 main 同步回来。
客户工作目录必须干净才能切换分支，不要在主线保留未跟踪的 customer-profile.json，否则本地运行会读取该文件。

## 标签与交付

- 公共版本：v<版本>，预览阶段采用 v<版本>-alpha.N 或 -rc.N。
- 客户版本：<客户代号>/v<版本>。
- 安装包必须从对应客户分支或客户标签构建，不直接从主线交付客户专属版本。
- 不移动已发布标签、不强制推送已交付分支。每次交付记录公共版本及客户提交号。

新客户从 main 创建 customer/<客户代号>，只添加自己的 customer-profile.json；不要从其他客户分支复制整个分支。
