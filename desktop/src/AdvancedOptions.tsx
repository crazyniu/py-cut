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
import { message, rpc, type JobKind, type Option } from "./api";
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
                label: option.label || option.name,
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
