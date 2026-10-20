import { useEffect, useRef, useState } from "react";
import { useDebouncedCallback } from "@mantine/hooks";
import {
  Alert,
  Button,
  Group,
  Loader,
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
  type Asset,
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
  const [assets, setAssets] = useState<Asset[]>([]);
  const [selection, setSelection] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [totalAssets, setTotalAssets] = useState(0);
  const [count, setCount] = useState<number | string>(1);
  const [output, setOutput] = useState("");
  const [folders, setFolders] = useState<string[]>([]);
  const [ready, setReady] = useState(false);
  const [visionReady, setVisionReady] = useState(kind !== "cover");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [staleCount, setStaleCount] = useState(0);
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
      .then(async ([items, saved, settings]) => {
        const selected = Array.isArray(saved.selection)
          ? saved.selection.filter(
              (value): value is string => typeof value === "string",
            )
          : [];
        const restored = await Promise.all(
          selected.map((asset_id) =>
            rpc<Asset>("assets.detail", {
              asset_id,
              project_id: project.id,
            }).catch(() => null),
          ),
        );
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
        const references = new Set(
          items
            .filter((item) => item.role === "reference")
            .map((item) => item.id),
        );
        const valid = restored.filter(
          (item): item is Asset =>
            item !== null &&
            Boolean(item.active_library_ids?.some((id) => references.has(id))),
        );
        setAssets(valid);
        setSelection(valid.map((item) => item.id));
        setStaleCount(selected.length - valid.length);
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
      selection,
      fission: Number(count) || 1,
      out_dir: output,
      input_dirs: folders,
    });
  }, [ready, selection, count, output, folders, save]);
  useEffect(() => {
    if (!ready || kind !== "montage") return;
    let disposed = false;
    const timeout = window.setTimeout(() => {
      setLoading(true);
      const sources = libraries.filter(
        (item) =>
          item.role === (kind === "montage" ? "reference" : "background"),
      );
      Promise.all(
        sources.map((library) =>
          rpc<{ items: Asset[]; total: number }>("assets.list", {
            library_id: library.id,
            query: search,
            removed: false,
            sort: "name",
            offset: 0,
            limit: 100,
            kind: "video",
          }),
        ),
      )
        .then((results) => {
          if (disposed) return;
          const found = [
            ...new Map(
              results
                .flatMap((result) => result.items)
                .map((asset) => [asset.id, asset]),
            ).values(),
          ];
          setAssets((previous) => [
            ...previous.filter(
              (asset) =>
                selection.includes(asset.id) &&
                !found.some((next) => next.id === asset.id),
            ),
            ...found,
          ]);
          setTotalAssets(
            results.reduce((total, result) => total + result.total, 0),
          );
        })
        .catch((reason) => {
          if (!disposed) setError(message(reason));
        })
        .finally(() => {
          if (!disposed) setLoading(false);
        });
    }, 250);
    return () => {
      disposed = true;
      window.clearTimeout(timeout);
    };
  }, [ready, libraries, kind, search, selection]);
  const total = selection.length * Number(count || 0);
  const offline =
    kind === "montage" &&
    assets.some((asset) => selection.includes(asset.id) && !asset.available);
  const backgroundReady = libraries.some(
    (library) => library.role === "background" && library.active_count > 0,
  );
  const referenceReady = libraries.some(
    (library) => library.role === "reference" && library.active_count > 0,
  );
  const configured =
    visionReady && (kind !== "montage" || (backgroundReady && referenceReady));
  const canSubmit =
    ready &&
    configured &&
    !submitting &&
    !offline &&
    (kind === "montage"
      ? Boolean(output.trim()) &&
        selection.length > 0 &&
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
              reference_ids: selection,
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
              ? staleCount
                ? `原先选择的 ${staleCount} 个话术已不在当前素材库，请先添加话术目录。`
                : "先在素材库添加话术视频目录。"
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
          {kind === "montage" && referenceReady && staleCount > 0 && (
            <Alert color="yellow">
              原先选择的 {staleCount} 个话术已不在当前素材库，请重新选择。
            </Alert>
          )}
          <Text size="sm" c="dimmed">
            {kind === "montage"
              ? "选择话术视频，设置每支话术的生成数量，再选择成片保存位置。"
              : "选择处理文件夹，自动包含所有子文件夹中的 MP4。源视频会保留。"}
          </Text>
          {kind === "montage" ? (
            <>
              <MultiSelect
                label="话术视频"
                disabled={submitting}
                nothingFoundMessage="未找到视频，请调整搜索或前往素材库添加话术目录。"
                placeholder="搜索并多选话术视频"
                data={assets
                  .filter((asset) => asset.kind === "video")
                  .map((asset) => ({
                    value: asset.id,
                    label: asset.path.split(/[\\/]/).slice(-2).join(" / "),
                    disabled: !asset.available,
                  }))}
                value={selection}
                onChange={setSelection}
                searchable
                searchValue={search}
                onSearchChange={setSearch}
                hidePickedOptions
                maxDropdownHeight={220}
                rightSection={loading ? <Loader size={14} /> : undefined}
              />
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
              {totalAssets > 100 && (
                <Text size="xs" c="dimmed">
                  输入名称查找更多视频。
                </Text>
              )}
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
                  ? "结果保存到下方指定目录。"
                  : `结果保存到各输入目录同级的“${kind === "cover" ? "字幕查重" : "去重"}”目录。`}
              </Text>
            </>
          )}
          <TextInput
            label="成片输出目录"
            disabled={submitting}
            description={
              kind === "montage"
                ? "本次任务会在此目录内创建独立批次文件夹。"
                : "可选；留空使用上述默认目录。"
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
          {offline && (
            <Text c="red" size="sm">
              所选视频已离线，请重新选择。
            </Text>
          )}
        </>
      )}
      <Group justify="flex-end" gap="sm" mt="sm" className="task-form-footer">
        {ready && (
          <>
            {" "}
            <Text size="sm" c="dimmed" mr="auto">
              {kind === "montage"
                ? `已选 ${selection.length} 个视频 · 共生成 ${total} 支`
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
