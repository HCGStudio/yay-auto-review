# yay-auto-review

[English](README.en.md) | 简体中文

在 **yay 执行 AUR 的 PKGBUILD 之前**调用 Codex 静态审查，以英文或简体中文展示结果，用户确认后才允许构建安装。支持 `yay -Syu`、指定 AUR 包安装、AUR 依赖与 split package；同一个 pkgbase 只审查一次。

使用 yay ≥ 13.0.0 的原生 Lua `AURPreInstall` 钩子，无需修改 yay 源码。

| 等级 | 判定 | 安装确认 |
| --- | --- | --- |
| 绿色 | 知名开源软件、来源已核实为官方、脚本审查完整且没有发现恶意行为或其他问题 | 输入 `y` |
| 白色 | 不知名开源软件、来源已核实为官方、脚本审查完整且没有发现恶意行为或其他问题 | 输入 `y` |
| 黄色 | 未发现恶意行为，但有其他问题，例如来源无法核实、闭源、完整性检查缺失、审查范围有限 | 输入完整包名 |
| 红色 | Codex 拒绝审查或发现恶意行为；调用失败、超时、无效响应、无法完整读取文件也按红色处理 | 阻止本次安装 |

“知名”由 Codex 提供声誉依据；项目名或托管平台本身不能证明来源官方。插件要求绿色/白色具有开源、官方来源、声誉及脚本安全证据，并检查结构化结果之间是否一致。证据不足时降为黄色。

终端输出使用相应颜色的 `●` 圆点标记结果，不显示颜色名称标签。输出重定向或设置 `NO_COLOR` 时使用不着色的圆点。

## 安装

### 从 AUR 安装

包名与仓库名一致：`yay-auto-review`。

```sh
yay -S yay-auto-review
codex login                 # 如尚未登录
yay-auto-review enable      # 以普通用户启用，不要 sudo
yay -Syu
```

AUR 包依赖官方仓库的 `openai-codex`、Python 和 Git，以及 yay。兼容 `yay-bin`、`yay-git` 的 `yay` provider；启用时要求实际 yay 版本不低于 13。包管理器只安装系统文件，每位用户通过 `enable` 接入自己的 yay 配置。升级后会自动加载新的共享 Lua 插件。

如果之前通过源码安装过同名命令，使用 `/usr/bin/yay-auto-review enable` 明确切换到系统安装的版本。

打包源码和维护步骤见 [packaging/aur](packaging/aur/README.md)。

### 从源码安装

需要 Arch Linux、yay ≥ 13.0.0、Python ≥ 3.11、Git，以及支持 `codex exec --output-schema --ignore-user-config --ignore-rules --ephemeral` 的 Codex CLI。Python 部分只使用标准库，不需要 pip 或 API SDK。

先确认 Codex 已登录：

```sh
codex login
```

在当前源码目录运行：

```sh
./install.sh
```

安装器会：

- 将程序安装到 `~/.local/bin/`，库放在 `~/.local/share/yay-auto-review/`。
- 将 Lua 插件放入安装前缀的 `share/yay-auto-review/`，并在 `$XDG_CONFIG_HOME/yay/yay-auto-review.lua`（默认 `~/.config/yay/yay-auto-review.lua`）生成指向共享插件的加载器。
- 在 `init.lua` 末尾添加托管的 `dofile(...)` 段；保留其他设置，修改已有配置前生成带时间戳的备份。重复安装会更新已有托管段。
- 使用绝对程序路径及 Python `-I` 启动器，避免从 AUR 构建目录加载同名 Python 模块。

无需 sudo。之后照常运行：

```sh
yay -Syu
yay -S aur-package-name
```

插件会审查本次计划中的每个 AUR pkgbase；官方仓库包仍由 yay/pacman 正常处理。审查失败可能发生在部分官方仓库操作之后，不意味着整项系统更新已回滚。

如需其他安装前缀：`./install.sh --prefix /absolute/path`。`install` 子命令需要从源码目录运行。已安装的程序可通过 `yay-auto-review enable` / `yay-auto-review disable` 启用或禁用。

## 语言 / i18n

界面、提示、错误和 Codex 报告仅在启动时读取一次 `LANG`，随后在进程内保持不变。`zh_CN.UTF-8` 等简体中文 locale 使用中文；未设置、空值、`C`、`POSIX` 或不支持的 locale 均回退为英文。AUR 包安装后的提示始终使用英文。

不支持运行时切换语言。`LC_ALL`、`LC_MESSAGES` 和旧的 `YAY_AUTO_REVIEW_LANG` 不影响语言；旧配置中的 `language` 字段仅为兼容保留，读取时会忽略。

```sh
LANG=en_US.UTF-8 yay-auto-review --help
LANG=zh_CN.UTF-8 yay-auto-review review /path/to/package --pkgbase package
LANG=zh_CN.UTF-8 yay -Syu
LANG=en_US.UTF-8 yay -Syu
```

审查缓存包含启动时确定的报告语言，不会复用另一语言的报告。JSON 的 `green`/`white`/`yellow`/`red` 等机器字段保持不变。

新增语言时，在 `yay_auto_review/locales/` 添加 UTF-8 JSON 目录，并在 `i18n.py` 注册语言、名称和 `normalize_language` 别名。英文消息是稳定键，缺失或格式不匹配的翻译回退为英文；支持拆分领域目录，如 `zh_CN.installer.json`。

## 使用体验

顺序为：解析目标和依赖 → 下载 AUR Git 仓库 → 自动审查并逐包确认 → yay 的 clean/diff/edit 菜单 → makepkg 前再次校验 → 构建安装。

输出示意（不是实际审查结果）：

```text
正在审查 example (a3c91e5b2d01)…
● example — 开源项目，来源与上游一致，未发现恶意脚本
  依据: …
允许构建并安装 example? [y/N]

● example — 开源项目，来源与上游一致，未发现恶意脚本
  跳过 review: example；上次审查未满 1 小时，AUR 提交及文件内容、策略和模型配置均未变化
允许构建并安装 example? [y/N]
```

缓存只跳过 Codex 调用，**不会跳过结果提示和本次用户确认**。确认读取 `/dev/tty`；`--noconfirm`、管道里的 `yes`、无终端运行都不能绕过它。默认回车取消；红色无自动放行选项。

如果用户在 yay 编辑菜单修改了文件，makepkg guard 会在执行前重新审查并确认。构建过程中 `pkgver()` 改写 PKGBUILD 后，下次执行 makepkg 也会重新审查包装文件。此时下载/生成的上游源码不在复核范围，结果至少为黄色并明确提示。

可以单独审查本地 AUR Git 仓库，不进行安装：

```sh
yay-auto-review review /absolute/path/to/package --pkgbase package
yay-auto-review review /absolute/path/to/package --pkgbase package --json
```

这两条命令同样会核对 AUR 官方远程的最新 HEAD，不接受本地旧提交作为当前审查对象。

## 缓存与文件校验

默认存放在 `$XDG_CACHE_HOME/yay-auto-review/`，未设置时使用 `~/.cache/yay-auto-review/`。

只有以下条件**全部满足**，才复用审查结果：

1. 审查完成距当前时间严格少于 **3600 秒**；恰好 1 小时、时间回拨产生的未来记录均不命中。
2. 向 `https://aur.archlinux.org/<pkgbase>.git` 查询的最新 HEAD 与本地及被审查提交一致。网络错误不能当作“没有更新”。
3. 所有被审查文件的路径、完整内容及可执行权限指纹一致。
4. pkgbase、AUR `last_modified`/版本元数据、审查策略版本、模型/调用命令配置及审查范围一致。

JSON 缓存只对当前用户可读写，使用文件锁和原子替换；并发审查同一对象不会重复调用 Codex。缓存不会因读取而续期。有效的红色报告也会缓存，命中后仍阻止安装；网络、进程、格式等技术错误不会缓存为正常报告，下次会重试。

每次 yay 调用创建新的 `builds/<session>/`，避免遗留文件和旧二进制包被套用新审查结果。guard 强制 makepkg 的 `PKGDEST`、`SRCDEST`、`SRCPKGDEST`、`LOGDEST`、`BUILDDIR` 使用该包的会话目录，因此会覆盖用户为这些变量配置的共享缓存路径。审查缓存独立保留，**构建缓存不跨会话复用**。

`receipts/<session>/` 保存本次用户确认及文件指纹，不能作为下次 yay 调用的确认。会话构建目录及确认记录保留，方便检查；在 yay 完全退出后，可删除对应的 `builds/<session>/` 和 `receipts/<session>/` 释放空间。只查询 yay 也可能创建空会话目录。

## 配置

可选：将 [config.example.toml](config.example.toml) 复制到 `~/.config/yay-auto-review/config.toml`（支持 `XDG_CONFIG_HOME`）。

```toml
codex = "codex"
# model = "your-model-id"
timeout_seconds = 300
makepkg = "/usr/bin/makepkg"
# cache_dir = "/absolute/path/to/private/review-cache"
```

不指定模型时使用 Codex CLI 默认模型。审查调用使用现有登录认证，但通过 `--ignore-user-config` 不加载个人 Codex 配置；自定义 provider/profile 不会自动沿用。需要自定义行为时，`codex` 可指向自行管理的可信 CLI 启动器；不要让启动器启用执行工具或取消隔离。

插件运行 Codex 时使用临时中立目录、只读沙箱、禁止请求执行许可，禁用 shell、插件、应用、hooks、多代理等执行能力；以 JSON 将完整包装文件送入提示词，并明确把仓库内容和网页当作不可信数据。通过实时网页查询核实来源，不执行 `source PKGBUILD`、`makepkg --printsrcinfo` 或包内脚本来收集审查输入。

## 审查边界

- 这是 **AUR 包装脚本静态审查**。不会全面审计下载的 tarball、二进制、上游整个 Git 仓库或构建时解析的传递依赖。绿色/白色表示满足审查条件，不是安全保证；可变分支、跳过校验等风险应显示在结果中。
- Codex 是模型，可能漏报、误报或受到提示注入影响。插件收紧工具和输出规则，但不能证明模型结论正确。通过确认后，makepkg 仍按普通用户权限执行，插件本身不是构建沙箱。
- 首次快照读取整个新 checkout（除 `.git`），包括 ignored/untracked 文件。符号链接、硬链接、特殊文件、非 UTF-8/二进制文件会阻止审查；每个文件最多 512 KiB、总计 2 MiB、最多 512 个文件。不会截断后给绿色。含图标等二进制包装资源的包目前也会被阻止。
- 下载开始后只复核原审查清单与当前 Git 跟踪文件；上游下载/生成产物不纳入清单。已执行的代码具有当前用户权限，插件无法防御已经控制该用户账户的恶意进程。哈希检查与 exec 之间也不是操作系统级原子执行边界。
- `--makepkg`、`--builddir`、`--mflags`、`--save` 会覆盖或持久化插件隔离设置，因此插件在 yay 启动时拒绝这些选项。makepkg 的 `-p`/`-D`/`--dir` 及目录参数缩写不能切换到未审查脚本；`BUILDFILE` 固定为 `PKGBUILD`。个人 `init.lua` 应从可信位置加载；不要在不可信目录使用 yay `--debug` 加载另一个 Lua 配置。
- 只保护已加载此插件的 yay AUR 安装流程；直接运行 makepkg/pacman、禁用 Lua 插件或改用其他 AUR helper 不在保护范围。

## 验证与开发

```sh
python3 -m unittest discover -s tests -v
```

测试使用临时 Git 仓库和假的 Codex/makepkg，覆盖分类校验、1 小时边界、缓存失效与并发、恶意输入隔离、Lua shell 传参、确认拒绝、文件变化再审查及安装器。安装了 yay 时还会通过临时配置执行真实 `yay -Pg`，验证原生 Lua 加载，不安装软件包。测试不调用真实模型。

## 卸载

先以普通用户运行 `yay-auto-review disable`，再通过包管理器卸载 `yay-auto-review`。源码安装则删除安装器写入的加载器、`yay-auto-review` 和 `yay-auto-review-makepkg` 两个启动器，以及 `~/.local/share/yay-auto-review/`。禁用操作只移除托管配置段，保留其他 yay 设置和审查历史。手动接入过 `require`/`dofile` 的用户需要自行移除对应行。缓存可在没有构建运行时另行清理。

## 许可证

Copyright (c) 2026 yay-auto-review contributors.

本项目采用 **GNU General Public License v3.0 only（SPDX: `GPL-3.0-only`）**，完整条款见 [LICENSE](LICENSE)。

## 接口依据

- [yay 原生 Lua 钩子文档](https://github.com/Jguer/yay/blob/next/doc/lua.md#aur-pre-install-hooks)：钩子位于仓库下载之后、菜单与源码下载/构建之前。
- [Codex 非交互命令文档](https://learn.chatgpt.com/docs/developer-commands#codex-exec)：`codex exec`、只读沙箱和结构化输出接口。本项目也核对了当前本机的 CLI help。
