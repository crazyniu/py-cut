import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Group,
  Loader,
  Paper,
  Select,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { IconRefresh } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import { message, rpc, type Agent } from "./api";
import { PageHeading, report } from "./common";
type Model = { id: string; name: string; is_default?: boolean };
export default function SettingsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [provider, setProvider] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [models, setModels] = useState<Model[]>([]);
  const [saved, setSaved] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [modelError, setModelError] = useState("");
  const [detecting, setDetecting] = useState(false);
  const [loadingModels, setLoadingModels] = useState(false);
  const [saving, setSaving] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let disposed = false;
    setDetecting(true);
    Promise.all([
      rpc<Agent[]>("agents.detect"),
      rpc<Record<string, string>>("settings.get"),
    ])
      .then(([items, values]) => {
        if (disposed) return;
        const installed = items.filter((item) => item.installed);
        setAgents(installed);
        setSaved(values);
        setProvider(
          installed.some((item) => item.id === values.vision_provider)
            ? values.vision_provider
            : installed[0]?.id || null,
        );
        setError("");
      })
      .catch((error) => {
        if (!disposed) setError(message(error));
      })
      .finally(() => {
        if (!disposed) setDetecting(false);
      });
    return () => {
      disposed = true;
    };
  }, [revision]);
  useEffect(() => {
    let disposed = false;
    setModels([]);
    setModel(null);
    setModelError("");
    if (!provider) return;
    setLoadingModels(true);
    rpc<Model[]>("agents.models", { provider })
      .then((items) => {
        if (disposed) return;
        setModels(items);
        setModel(
          provider === saved.vision_provider &&
            items.some((item) => item.id === saved.vision_model)
            ? saved.vision_model
            : null,
        );
        if (!items.length)
          setModelError("Codex 没有返回可用模型，请检查登录状态后重试。");
      })
      .catch((error) => {
        if (!disposed) setModelError(message(error));
      })
      .finally(() => {
        if (!disposed) setLoadingModels(false);
      });
    return () => {
      disposed = true;
    };
  }, [provider, saved, revision]);
  async function saveAgent() {
    if (!provider || !model || saving) return;
    setSaving(true);
    try {
      const values = {
        vision_provider: provider,
        vision_model: model,
        agent_path: "",
      };
      await rpc("settings.save", { values });
      setSaved(values);
      notifications.show({ message: "全局 Codex 设置已保存", color: "teal" });
    } catch (error) {
      report(error);
    } finally {
      setSaving(false);
    }
  }
  const agent = agents.find((item) => item.id === provider);
  return (
    <>
      <PageHeading
        title="全局设置"
        description="识别模型对所有项目生效，素材目录在各项目的素材库管理。"
      />
      {error && (
        <Alert color="red" mb="md">
          {error}
        </Alert>
      )}
      <Stack gap="lg">
        <Paper withBorder p="lg">
          <Stack>
            <Group gap="md">
              <Title order={3} size="h5">
                Codex 设置
              </Title>
              <Button
                variant="default"
                size="xs"
                loading={detecting}
                leftSection={<IconRefresh size={14} />}
                onClick={() => setRevision((value) => value + 1)}
              >
                重新检测
              </Button>
            </Group>
            <Text size="sm" c="dimmed">
              字幕擦除通过本机 Codex 识别字幕。选择已登录账号可用的模型后保存。
            </Text>
            <Group align="flex-end" gap="md" className="settings-form-row">
              <TextInput
                w={220}
                label="智能工具"
                value={
                  detecting
                    ? "正在检测…"
                    : agent?.available
                      ? "Codex · 已就绪"
                      : "Codex · 未就绪"
                }
                readOnly
              />
              <Select
                w={380}
                label="使用模型"
                placeholder={loadingModels ? "正在读取模型…" : "请选择模型"}
                data={models.map((item) => ({
                  value: item.id,
                  label: item.name,
                }))}
                value={model}
                onChange={setModel}
                allowDeselect={false}
                searchable
                disabled={loadingModels || detecting || saving || !provider}
                rightSection={loadingModels ? <Loader size={14} /> : undefined}
              />
              <Button
                onClick={saveAgent}
                disabled={
                  loadingModels ||
                  detecting ||
                  !model ||
                  !agent?.available ||
                  (model === saved.vision_model &&
                    provider === saved.vision_provider)
                }
                loading={saving}
              >
                {model &&
                model === saved.vision_model &&
                provider === saved.vision_provider
                  ? "已保存"
                  : "保存设置"}
              </Button>
            </Group>
            {modelError && <Alert color="orange">{modelError}</Alert>}
            {agent?.supports_vision && !agent.available && (
              <Text size="sm" c="orange">
                请先登录 {agent.name}，然后重新检测。
              </Text>
            )}
          </Stack>
        </Paper>
      </Stack>
    </>
  );
}
