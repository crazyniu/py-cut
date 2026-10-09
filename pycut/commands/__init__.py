"""pycut.commands —— 命令实现层。

每个命令模块暴露两个函数：
  - register(subparsers)：向 argparse 注册自己的子命令与参数。
  - run(args)：执行（args 为该子命令的命名空间）。
命令注册表集中维护在 cli.py，便于以后接入 TUI（同一套 run 可复用）。
"""
