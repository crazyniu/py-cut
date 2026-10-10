# Dedup 本地账号预设（JSON v1）

本功能只保存**非敏感的视频处理偏好**，不登录平台、不上传视频、不保存或使用平台账号凭证。原 `dedup` 不指定账号时行为、默认档位和现有 CLI 参数均保持不变。

## 使用

创建本地 JSON，例如 `profiles.local.json`：

```json
{
  "version": 1,
  "accounts": {
    "main_channel": {
      "preset": "light",
      "seed": 101,
      "border": false,
      "psnr_min": 22.0
    },
    "second_channel": {
      "preset": "standard",
      "seed": 202,
      "border": true,
      "badge": true
    }
  }
}
```

```bash
./run.sh dedup --file /path/to/authorized.mp4 \
  --profile-file profiles.local.json --account main_channel

# 命令行显式参数优先于账号预设：
./run.sh dedup --file /path/to/authorized.mp4 \
  --profile-file profiles.local.json --account second_channel \
  --preset light --no-border --seed 888
```

执行顺序：**显式 CLI 参数 > 指定账号的 JSON 设置 > dedup 原有 CLI 默认值**。布尔值可使用 `--mirror/--no-mirror`、`--speed/--no-speed`、`--border/--no-border`、`--badge/--no-badge` 进行显式覆盖；未提供这些 CLI 参数才读取账号对应值。切换账号不会悄悄修改另一个账号。

## JSON 合约

- 顶层只能有 `version: 1` 与 `accounts`。`accounts` 为非空对象，最多 100 个账号别名。
- 账号别名长度 1～64，仅允许英文字母、数字、点、下划线、连字符，首字符必须是英文字母或数字。
- 每个账号对象至少一个配置项；可用字段只有：`preset`（`light` / `standard` / `aggressive`）、`seed`（0～2³²−1 的整数）、`mirror`、`speed`、`border`、`badge`（JSON 布尔值）、`phash_min`（0～64）、`psnr_min`（0～100）、`workers`（0～64）。
- JSON 重复键、未知键、非法数值、未知账号、缺失 `--account`/`--profile-file` 或超过 64 KiB 的文件均以非零退出码拒绝运行，不会偷偷回退至其他账号。
- **禁止**在 JSON 内加入账号密码、Cookie、令牌、API Key、网关地址、账号真实身份或任意 FFmpeg 命令。未知字段会被拒绝，错误日志不回显其值。

没有显式 `seed` 或设置为 0 时，保留现有按源文件路径生成 seed 的规则。固定输入路径、seed、档位及开关能够复现参数选择；运行环境、实际视频编码及 FFmpeg 版本仍会影响二进制输出。

若启用 JSON 报告，每个结果的 `account_profile` 字段记录账号**别名**、配置版本、最终生效的白名单参数 `effective` 及其来源 `sources`（`cli`、`account`、`default`），以便审查冲突与复现设置；不记录配置文件的路径或凭据。`--no-report` 仍可关闭报告。

## 与已有处理流程的关系

预设解析仅发生在 `pycut/commands/dedup_cmd.py`，复用 `pycut/core/dedup_presets.py` 的档位逻辑、`DedupOptions`、`process_batch` 和 `write_report`，**不新增编码器、不改变旧档位参数、不改变质量门禁及输出覆盖行为**。处理范围仍由 `--file`、`--src-dir`、`--all` 控制；默认输出目录和 `--force`/`--in-place` 安全限制不属于账号预设字段。
