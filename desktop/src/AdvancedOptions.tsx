import {
  Accordion,
  Checkbox,
  NumberInput,
  Select,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { useEffect, useState } from "react";
import { IconFolderOpen } from "@tabler/icons-react";
import { ActionIcon } from "@mantine/core";
import { chooseFile, message, rpc, type JobKind, type Option } from "./api";
import { report } from "./common";
const labels: Record<string, string> = {
  seed: "随机种子", allow_landscape: "允许横屏素材", pip_slot: "每段画中画时长（秒）",
  crf: "画质 CRF", mirror_chance: "随机镜像概率", speed_max: "随机加速上限",
  sticker_chance: "贴纸出现概率", seam_fade: "段间淡化时长（秒）", pip_gap: "画中画段间留空",
  noise_db: "静音判定阈值（dB）", min_silence: "最短静音时长（秒）",
  min_clip: "最短素材时长（秒）", max_clip: "最长素材时长（秒）", pip_scale: "画中画宽度比例",
  workers: "并发数", profile_file: "账号配置文件", account: "账号预设", preset: "去重强度",
  mirror: "允许水平镜像", speed: "允许微变速", vary_bitrate: "视频码率扰动",
  ken_burns: "缓慢缩放与平移", border: "模糊边框", badge: "右下角色块",
  logo: "角标图片", sticker: "贴纸图片", delogo: "水印遮盖区域（x,y,w,h）",
  bgm: "背景音乐", ambience: "环境音", voiceover: "替换配音", original_gain: "原声增益",
  bgm_gain: "背景音乐增益", ambience_gain: "环境音增益", voice_gain: "配音增益",
  audio_fade_out: "音轨末尾淡出（秒）", check: "仅自检已有结果", no_report: "不生成检测报告",
  vmaf: "计算 VMAF", vmaf_min: "VMAF 下限", phash_min: "pHash 距离下限", psnr_min: "PSNR 下限（dB）",
  force: "重新处理已有结果", samples: "每段抽帧数量", dry_run: "仅检测字幕区域",
};
const fileOptions = new Set(["profile_file", "logo", "sticker", "bgm", "ambience", "voiceover"]);
const managed = new Set([
  "intro_enabled",
  "fission",
  "out_dir",
  "no_pip",
  "no_sub",
  "no_audio",
  "no_sticker",
  "step",
  "source",
  "clip_dir",
  "dedup_dir",
  "intro_dir",
  "sticker_dir",
  "file",
  "all",
  "src_dir",
  "srt",
  "batch_dir",
  "in_place",
  "fill_ratio",
  "gateway",
  "llm_url",
  "llm_key",
  "llm_model",
  "no_fallback",
]);
export default function AdvancedOptions({
  kind,
  values,
  onChange,
}: {
  kind: JobKind;
  values: Record<string, unknown>;
  onChange: (values: Record<string, unknown>) => void;
}) {
  const [options, setOptions] = useState<Option[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let disposed = false;
    rpc<Option[]>("jobs.options", { kind })
      .then((result) => {
        if (!disposed)
          setOptions(result.filter((item) => !managed.has(item.name)));
      })
      .catch((error) => {
        if (!disposed) setError(message(error));
      });
    return () => {
      disposed = true;
    };
  }, [kind]);
  function change(name: string, value: unknown) {
    onChange({ ...values, [name]: value });
  }
  return (
    <Accordion variant="default">
      <Accordion.Item value="advanced">
        <Accordion.Control>高级参数</Accordion.Control>
        <Accordion.Panel>
          <Stack gap="md">
            {error && (
              <Text size="xs" c="red">
                参数无法读取：{error}
              </Text>
            )}
            {options.map((option) => {
              const value = values[option.name] ?? option.default;
              const common = {
                label: labels[option.name] || option.label || option.name,
                description: option.help,
              };
              return option.type === "boolean" ||
                typeof option.default === "boolean" ? (
                <Checkbox
                  key={option.name}
                  {...common}
                  checked={Boolean(value)}
                  onChange={(event) =>
                    change(option.name, event.currentTarget.checked)
                  }
                />
              ) : option.choices?.length ? (
                <Select
                  key={option.name}
                  {...common}
                  data={option.choices.map((item) => ({
                    value: String(item),
                    label: String(item),
                  }))}
                  value={value == null ? null : String(value)}
                  onChange={(item) => change(option.name, item)}
                  allowDeselect={false}
                />
              ) : ["number", "integer", "int", "float"].includes(option.type) ||
                typeof option.default === "number" ? (
                <NumberInput
                  key={option.name}
                  {...common}
                  value={typeof value === "number" ? value : ""}
                  onChange={(item) =>
                    change(option.name, item === "" ? null : Number(item))
                  }
                  allowDecimal={
                    option.type !== "integer" && option.type !== "int"
                  }
                />
              ) : (
                <TextInput
                  key={option.name}
                  {...common}
                  value={value == null ? "" : String(value)}
                  rightSection={fileOptions.has(option.name) ? (
                    <ActionIcon variant="subtle" aria-label={`选择${labels[option.name]}`} onClick={async () => {
                      try {
                        const path = await chooseFile(`选择${labels[option.name]}`);
                        if (path) change(option.name, path);
                      } catch (error) { report(error); }
                    }}><IconFolderOpen size={16} /></ActionIcon>
                  ) : undefined}
                  onChange={(event) =>
                    change(option.name, event.currentTarget.value)
                  }
                />
              );
            })}
            <Text size="xs" c="dimmed">
              未修改的参数沿用现有处理默认值。
            </Text>
          </Stack>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}
