import { useEffect, useRef, useState } from "react";
import { useDebouncedCallback } from "@mantine/hooks";
import {
  Alert,
  Button,
  Group,
  MultiSelect,
  NumberInput,
  TextInput,
  Stack,
  Text,
} from "@mantine/core";
import { IconFolderOpen, IconPlayerPlay } from "@tabler/icons-react";
import {
  chooseDirectories,
  chooseDirectory,
  kinds,
  message,
  rpc,
  type Job,
  type JobKind,
  type Library,
  type Project,
  type Agent,
} from "./api";
import { report, LoadingPage } from "./common";

export default function TaskPage({
  project,
  kind,
  onSettings,
  onLibrary,
  onSubmitted,
  onCancel,
  onBusyChange,
}: {
  project: Project;
  kind: JobKind;
  onSettings: () => void;
  onLibrary: () => void;
  onSubmitted: (job: Job) => void;
  onCancel: () => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [count, setCount] = useState<number | string>(1);
  const [output, setOutput] = useState("");
  const [folders, setFolders] = useState<string[]>([]);
  const [ready, setReady] = useState(false);
  const [visionReady, setVisionReady] = useState(kind !== "cover");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const skipFirstSave = useRef(true);
  const save = useDebouncedCallback(
    (values: Record<string, unknown>) => {
      void rpc("workbench.save", {
        project_id: project.id,
        kind,
        values,
      }).catch(report);
    },
    { delay: 400, flushOnUnmount: true },
  );
  useEffect(() => {
    let disposed = false;
    setError("");
    Promise.all([
      rpc<Library[]>("libraries.list", { project_id: project.id }),
      rpc<Record<string, unknown>>("workbench.get", {
        project_id: project.id,
        kind,
      }),
      kind === "cover"
        ? Promise.all([
            rpc<Record<string, string>>("settings.get"),
            rpc<Agent[]>("agents.detect"),
          ])
        : Promise.resolve(null),
    ])
      .then(([items, saved, settings]) => {
        if (disposed) return;
        setLibraries(items);
        setFolders(
          Array.isArray(saved.input_dirs)
            ? saved.input_dirs.filter(
                (value): value is string => typeof value === "string",
              )
            : [
                ...new Set(
                  items
                    .filter((item) => item.role === "background")
                    .flatMap((item) => item.roots),
                ),
              ],
        );
        if (typeof saved.fission === "number") setCount(saved.fission);
        if (typeof saved.out_dir === "string") setOutput(saved.out_dir);
        if (settings)
          setVisionReady(
            Boolean(settings[0].vision_model) &&
              settings[1].some(
                (agent) => agent.id === "codex" && agent.available,
              ),
          );
        setReady(true);
      })
      .catch((reason) => {
        if (!disposed) setError(message(reason));
      });
    return () => {
      disposed = true;
    };
  }, [project.id, kind, retry]);
  useEffect(() => {
    if (!ready) return;
    if (skipFirstSave.current) {
      skipFirstSave.current = false;
      return;
    }
    save({
      fission: Number(count) || 1,
      out_dir: output,
      input_dirs: folders,
    });
  }, [ready, count, output, folders, save]);
  const referenceCount = libraries.reduce(
    (total, library) => total + (library.role === "reference" ? library.included_video_count : 0), 0,
  );
  const total = referenceCount * Number(count || 0);
  const backgroundReady = libraries.some(
    (library) => library.role === "background" && library.active_count > 0,
  );
  const referenceReady = referenceCount > 0;
  const configured =
    visionReady && (kind !== "montage" || (backgroundReady && referenceReady));
  const canSubmit =
    ready &&
    configured &&
    !submitting &&
    (kind === "montage"
      ? Boolean(output.trim()) &&
        referenceCount > 0 &&
        Number.isInteger(Number(count)) &&
        Number(count) >= 1 &&
        Number(count) <= 99
      : folders.length > 0);
  async function submit() {
    if (!canSubmit) return;
    setSubmitting(true);
    onBusyChange(true);
    setError("");
    try {
      const job = await rpc<Job>("jobs.submit", {
        project_id: project.id,
        kind,
        ...(kind === "montage"
          ? {
              options: { fission: Number(count), out_dir: output.trim() },
            }
          : {
              input_dirs: folders,
              options: output.trim() ? { out_dir: output.trim() } : {},
            }),
      });
      onSubmitted(job);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setSubmitting(false);
      onBusyChange(false);
    }
  }
  return (
    <Stack gap="md">
      {error && (
        <Alert color="red" title="任务暂时无法开始">
          {error}
          {!ready && (
            <Button
              variant="subtle"
              onClick={() => setRetry((value) => value + 1)}
            >
              重新读取
            </Button>
          )}
        </Alert>
      )}
      {!ready && !error && <LoadingPage label="正在读取任务配置…" />}
      {ready && !configured && (
        <Alert color="yellow">
          {!visionReady
            ? "字幕擦除需要已登录的 Codex 和已保存的识别模型。"
            : !referenceReady
              ? "话术库没有未排除的视频，请先在素材库选中话术。"
              : "先在素材库配置背景切片目录。"}
          <Button
            variant="subtle"
            size="xs"
            onClick={!visionReady ? onSettings : onLibrary}
          >
            {!visionReady ? "前往设置" : "管理素材库"}
          </Button>
        </Alert>
      )}
      {ready && (
        <>
          <Text size="sm" c="dimmed">
            {kind === "montage"
              ? "自动使用话术库中全部未排除的视频，设置每支话术的生成数量和保存位置。"
              : "选择处理文件夹，自动包含所有子文件夹中的 MP4。源视频会保留。"}
          </Text>
          {kind === "montage" ? (
            <>
              <Group justify="space-between">
                <Text size="sm">已选 {referenceCount} 个话术</Text>
                <Button variant="subtle" size="xs" onClick={onLibrary} disabled={submitting}>管理话术</Button>
              </Group>
              <NumberInput
                label="每个视频生成数量"
                description="每支所选话术生成 1–99 支独立成片。"
                disabled={submitting}
                min={1}
                max={99}
                allowDecimal={false}
                clampBehavior="strict"
                value={count}
                onChange={setCount}
              />
              <Text size="xs" c="dimmed">
                自动使用项目中的背景切片
                {libraries.some(
                  (item) => item.role === "pip" && item.active_count,
                )
                  ? "、画中画"
                  : ""}
                {libraries.some(
                  (item) => item.role === "intro" && item.active_count,
                )
                  ? "、前贴"
                  : ""}
                ；素材目录在素材库管理。
              </Text>
            </>
          ) : (
            <>
              <MultiSelect
                label="处理文件夹"
                disabled={submitting}
                placeholder="选择已有目录或点击选择文件夹"
                data={[
                  ...new Set([
                    ...libraries
                      .filter((library) => library.role === "background")
                      .flatMap((library) => library.roots),
                    ...folders,
                  ]),
                ]}
                value={folders}
                onChange={setFolders}
                searchable
              />
              <Group className="task-form-options">
                <Button
                  variant="default"
                  disabled={submitting}
                  leftSection={<IconFolderOpen size={16} />}
                  onClick={async () => {
                    try {
                      const chosen = await chooseDirectories();
                      if (chosen.length)
                        setFolders((previous) => [
                          ...new Set([...previous, ...chosen]),
                        ]);
                    } catch (reason) {
                      setError(message(reason));
                    }
                  }}
                >
                  选择文件夹
                </Button>
              </Group>
              <Text size="xs" c="dimmed">
                {output.trim()
                  ? "每次任务在下方目录内创建独立文件夹。"
                  : `每次任务在首个输入目录同级的“${kind === "cover" ? "字幕查重" : "去重"}”目录内创建独立文件夹。`}
              </Text>
            </>
          )}
          <TextInput
            label="成片输出目录"
            disabled={submitting}
            description={
              kind === "montage"
                ? "本次任务会在此目录内创建独立批次文件夹。"
                : "每次任务创建独立文件夹；留空使用源目录同级位置。"
            }
            placeholder={
              kind === "montage" ? "选择成片保存位置" : "留空时保存到源目录同级"
            }
            value={output}
            onChange={(event) => setOutput(event.currentTarget.value)}
            rightSection={
              <Button
                variant="subtle"
                size="compact-xs"
                disabled={submitting}
                aria-label="选择成片输出目录"
                onClick={async () => {
                  try {
                    const path = await chooseDirectory();
                    if (path) setOutput(path);
                  } catch (reason) {
                    setError(message(reason));
                  }
                }}
              >
                <IconFolderOpen size={16} />
              </Button>
            }
          />
        </>
      )}
      <Group justify="flex-end" gap="sm" mt="sm" className="task-form-footer">
        {ready && (
          <>
            {" "}
            <Text size="sm" c="dimmed" mr="auto">
              {kind === "montage"
                ? `已选 ${referenceCount} 个话术 · 共生成 ${total} 支`
                : `已选 ${folders.length} 个文件夹`}
            </Text>
          </>
        )}
        <Button variant="default" disabled={submitting} onClick={onCancel}>
          取消
        </Button>
        <Button
          leftSection={<IconPlayerPlay size={16} />}
          disabled={!canSubmit}
          onClick={submit}
          loading={submitting}
        >
          开始{kinds[kind]}
        </Button>
      </Group>
    </Stack>
  );
}
