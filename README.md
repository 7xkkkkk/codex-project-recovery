# Codex Project Recovery

恢复 Windows 版 Codex Desktop 丢失的普通本地项目入口。

适用于这样的情况：侧边栏里的项目消失了，但项目目录、历史对话和数据库中的原项目记录仍在。工具会核对这些记录，生成恢复计划，再由你选择要恢复的项目。

需要 **Windows 和 Python 3.10+**，无需安装第三方 Python 包。这是社区工具，依赖客户端内部状态格式；遇到不支持的结构会报错停止。

## 先确认项目真的丢了

检查侧边栏的**置顶区域、折叠分组和 ChatGPT 视图**。入口位置变化不需要修复注册表。

关联 ChatGPT 的项目（`g-p-…` 或 `.chatgpt-projects` 下的目录）会被本工具跳过。在 Windows 客户端 26.903.9818.0 的一次排查中，这类入口受到视图过滤影响；切换视图后才能找到。其他版本的行为可能不同。

## 使用

下载或克隆仓库，在仓库根目录打开**独立 PowerShell 窗口**。后面需要退出 Codex，因此不要使用 Codex 内置终端。

### 1. 检查现有记录

```powershell
python recover.py audit --out .local/audit.json
```

打开生成的 JSON，查看：

- `current_projects`：当前已注册的项目。
- `candidates`：数据库中的项目记录及检查结果。
- `eligible` 和 `reasons`：是否可以恢复，以及跳过原因。

确认候选项目的名称和 `rootPaths` 正确，再复制 `eligible: true` 项的 `legacy_id`。

如需同时汇总历史会话元数据里的路径，在 audit 命令末尾加 `--sessions`。这些路径仅供排查，不会直接变成恢复候选。

### 2. 生成并检查恢复计划

将下面的 `<legacy-id>` 换成报告中的实际值。恢复多个项目时，重复传入 `--select`。

```powershell
python recover.py plan --select "<legacy-id>" --out .local/plan.json
python recover.py check .local/plan.json
```

查看 `.local/plan.json` 中准备写入的记录。前两步只读取客户端状态，报告和计划保存在仓库的 `.local` 目录。

### 3. 退出应用后执行

完全退出 Codex、ChatGPT 和 Codex CLI，保留独立 PowerShell 窗口，然后运行：

```powershell
python recover.py apply .local/plan.json --confirm
```

工具会检查进程、重新核对计划、创建备份，然后原子替换状态文件并读回验证。若记录已经变化，请重新生成计划。

看到 `SUCCESS` 后，重新打开 Codex，确认项目入口和目录正确。历史任务的归属不会被修改，需要在界面中另行核对。

## 恢复范围

工具读取 Codex 数据目录中的两个文件：

| 文件 | 用途 |
| --- | --- |
| `.codex-global-state.json` | 读取现有入口；补回选中项目的记录、排序和 ID 映射 |
| `state_5.sqlite` | 只读核对已有项目、根目录和历史 ID |

恢复必须有现存的后端项目记录、唯一历史 ID 和有效目录作为依据。工具保留原有身份，不生成 ID，也不创建后端项目。

Worktree、云端关联项目、目录重叠、ID 存在歧义或后端实体已删除的记录会被跳过。会话、Git 数据、worktree 文件和云端记录均不在写入范围内。

默认数据目录为 `%USERPROFILE%\.codex`；设置了 `CODEX_HOME` 时优先使用该值。也可以在子命令前指定：

```powershell
python recover.py --home "D:\MyCodexHome" audit --out .local/audit.json
```

`check` 和 `apply` 使用计划中保存的目标目录。

## 备份与排错

备份保存在 `.local/backups/<时间戳>/`，包含原始 JSON、SQLite 一致性备份和成功写入后的 `result.json`。

| 提示 | 处理方式 |
| --- | --- |
| `Unsupported … schema` | 当前客户端结构不受支持，停止使用该版本工具 |
| `Not an eligible unique candidate` | 查看 audit 报告中的跳过原因 |
| `Plan or underlying records changed` | 重新 audit、选择项目并生成计划 |
| `Close Codex/ChatGPT…` | 退出相关进程后，从独立终端执行 |

如需回退，先退出应用并保留当前状态，再审查备份之后的新项目和设置。工具只写 JSON，**不要直接用旧 SQLite 覆盖当前数据库**。目前没有自动回退命令。

报告、计划和备份包含本机路径及项目 ID，请留在本地；仓库的 `.gitignore` 已排除默认输出目录。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试使用临时目录与模拟数据库，覆盖候选筛选、过期计划、备份和原子写入等逻辑。GitHub Actions 在 Windows 上使用 Python 3.10 和 3.13 运行测试。Linux 和 macOS 不在当前支持范围内。

## License

[MIT](LICENSE)
