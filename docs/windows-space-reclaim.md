# 一次执行清理和虚拟磁盘压缩

本机入口是 `D:\reclaim-auto-agents-space.cmd`。双击运行并接受 Windows
管理员提示即可；也可以在 PowerShell 执行：

```powershell
& 'D:\reclaim-auto-agents-space.cmd'
```

运行前结束 auto-agents、Codex、Claude 等任务，并关闭其他 WSL 工作。
脚本会检查仍在运行的 agent 与项目执行锁；发现占用就退出。压缩阶段会
关闭 Docker Desktop 和全部 WSL 发行版，因此不要同时打开 WSL 终端或
通过文件管理器访问 Linux 文件。首次运行的压缩可能需要较长时间。

执行顺序是：

1. 检查任务占用，记录原先运行的 Docker 容器。
2. 清理已登记、符合保留和引用规则的中间产物；回收已完成监督作业的副本、
   闲置且有归属标签的旧工具镜像、无打开文件且超过一天的本用户 pytest 夹具。
3. 执行 Ubuntu `fstrim`，向虚拟磁盘报告已经空闲的块。
4. 停止原有容器、Docker Desktop 与 WSL；检查虚拟磁盘不在使用中。
5. 优先使用 `Optimize-VHD`，不可用时使用 DiskPart，压缩注册的 Ubuntu
   和 Docker 数据盘。
6. 在 `finally` 中恢复 Ubuntu；原先 Docker 正在运行时，恢复 Docker 及
   原容器。记录压缩前后的文件大小、Windows 空闲量和错误信息。

清理或 Ubuntu TRIM 失败时不会进入停服务/压缩。压缩失败后仍尝试恢复服务；
恢复失败会显示 `needs-attention`，并保留原容器 ID，不能被显示为成功。
脚本不会中途杀死压缩进程。重复执行有 Windows 互斥锁保护。

预览只读取发行版注册表和磁盘路径，不清理、不 TRIM、不停服务、不压缩：

```powershell
& 'D:\reclaim-auto-agents-space.cmd' --preview
```

日志在 `D:\auto-agents-storage-reclaim\日期时间\`，包括 `status.json`、
`cleanup.log`、`result.json`、磁盘压缩日志和 `running-containers.json`。
默认发行版为 `Ubuntu-20.04`，Linux 用户为 `fuli`，Python 来自
`/home/fuli/miniconda3/envs/autoagents/bin/python`，维护项目为 SDGP。
参数保存在同目录 PowerShell 文件顶部，可用 PowerShell 参数覆盖。

此脚本使用内置清理器，不凭目录名称删除任意历史 `/tmp` 或旧任务证据。
当前进度、未知请求、迁移备份、用户代码、账号、应用数据库、真实媒体和
用户 Docker 数据卷保留。旧归档或停止任务的唯一恢复证据，需要像本次清理
一样另行核对，不能让日常脚本无条件删除。

发行版磁盘路径从当前用户的 WSL 注册表读取，Windows 双击入口在 D 盘，
避免 WSL 关闭后脚本本身失联。压缩要求磁盘已卸载或只读挂载，遵循
[Microsoft DiskPart 文档](https://learn.microsoft.com/windows-server/administration/windows-commands/compact-vdisk)；
WSL 路径定位参考
[Microsoft WSL 磁盘管理文档](https://learn.microsoft.com/en-us/windows/wsl/disk-space)。

仓库源文件为 `scripts/reclaim-auto-agents-space.cmd`、
`scripts/reclaim-auto-agents-space.ps1` 和 `scripts/reclaim_wsl_space.py`。
本次仅做 Python 测试、实际 PowerShell 5.1 语法检查、无副作用预览与
替换实际维护函数的失败路径测试，未实际压缩虚拟磁盘。
