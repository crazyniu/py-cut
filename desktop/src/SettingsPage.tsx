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
import { IconFolderOpen, IconRefresh } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import { chooseDirectory, message, rpc, type Agent, type Project } from "./api";
import { PageHeading, report } from "./common";
type Model = { id: string; name: string; is_default?: boolean };
export default function SettingsPage({
  project,
  onProjectChange,
}: {
  project: Project;
  onProjectChange: () => void;
}) {
  const [name, setName] = useState(project.name);
  const [output, setOutput] = useState(project.output_dir || "");
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
    setName(project.name);
    setOutput(project.output_dir || "");
  }, [project.name, project.output_dir]);
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
          setModelError(
            "Codex 没有返回可用模型，请检查登录状态后重试。",
          );
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
  async function saveProject() {
    setSaving(true);
    try {
      await rpc("projects.update", {
        project_id: project.id,
        name: name.trim(),
        output_dir: output.trim(),
      });
      onProjectChange();
      notifications.show({ message: "项目设置已保存", color: "teal" });
    } catch (error) {
      report(error);
    } finally {
      setSaving(false);
    }
  }
  async function saveAgent() {
    if (!provider || !model) return;
    setSaving(true);
    try {
      const values = {
        vision_provider: provider,
        vision_model: model,
        agent_path: "",
      };
      await rpc("settings.save", { values });
      setSaved(values);
      notifications.show({ message: "识别工具和模型已保存", color: "teal" });
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
        title="设置"
        description="设置项目名称、保存位置和Codex 设置。"
      />
      {error && (
        <Alert color="red" mb="md">
          {error}
        </Alert>
      )}
      <Stack gap="lg">
        <Paper withBorder p="lg">
          <Stack>
            <Title order={3} size="h5">
              项目设置
            </Title>
            <Group align="flex-end" gap="md">
              <TextInput
                w={220}
                label="项目名称"
                value={name}
                onChange={(event) => setName(event.currentTarget.value)}
              />
              <TextInput
                w={380}
                label="默认成片输出目录"
                value={output}
                onChange={(event) => setOutput(event.currentTarget.value)}
                rightSection={
                  <Button
                    variant="subtle"
                    size="compact-xs"
                    aria-label="选择默认输出目录"
                    onClick={async () => {
                      try {
                        const path = await chooseDirectory();
                        if (path) setOutput(path);
                      } catch (error) {
                        report(error);
                      }
                    }}
                  >
                    <IconFolderOpen size={16} />
                  </Button>
                }
              />

              <Button
                onClick={saveProject}
                disabled={!name.trim()}
                loading={saving}
              >
                保存项目设置
              </Button>
            </Group>
          </Stack>
        </Paper>
        <Paper withBorder p="lg">
          <Stack>
            <Group justify="space-between">
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
              Codex 为本机智能功能提供服务。请选择要使用的模型。
            </Text>
            <Group align="flex-end" gap="md">
              <Select
                w={180}
                label="智能工具"
                placeholder={detecting ? "正在检测…" : "未检测到 Codex"}
                data={agents.map((item) => ({
                  value: item.id,
                  label: item.name,
                }))}
                value={provider}
                onChange={setProvider}
                allowDeselect={false}
                disabled={detecting}
              />
              <Select
                w={300}
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
                disabled={loadingModels || !provider}
                rightSection={loadingModels ? <Loader size={14} /> : undefined}
              />
              <Button
                onClick={saveAgent}
                disabled={!model || !agent?.available}
                loading={saving}
              >
                保存 Codex 设置
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
