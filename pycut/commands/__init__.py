"""pycut.commands —— 命令实现层。

每个命令模块暴露两个函数：
  - register(subparsers)：向 argparse 注册自己的子命令与参数。
  - run(args)：执行（args 为该子命令的命名空间）。
命令注册表集中维护在 cli.py；TUI 通过 cli.build_parser() 读取同一份参数定义，
复用同一套 run()，因此 CLI 与 TUI 天然保持一致。
"""
