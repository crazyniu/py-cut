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
} from "./api";
import { report } from "./common";

export default function TaskPage({
  project,
  kind,
  onSettings,
  onLibrary,
  onSubmitted,
}: {
  project: Project;
  kind: JobKind;
  onSettings: () => void;
  onLibrary: () => void;
  onSubmitted: (job: Job) => void;
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
    Promise.all([
      rpc<Library[]>("libraries.list", { project_id: project.id }),
      rpc<Record<string, unknown>>("workbench.get", {
        project_id: project.id,
        kind,
      }),
      kind === "cover"
        ? rpc<Record<string, string>>("settings.get")
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
        setAssets(restored.filter((item): item is Asset => item !== null));
        setSelection(selected);
        if (typeof saved.fission === "number") setCount(saved.fission);
        if (typeof saved.out_dir === "string") setOutput(saved.out_dir);
        if (settings) setVisionReady(Boolean(settings.vision_model));
        setReady(true);
      })
      .catch((reason) => {
        if (!disposed) setError(message(reason));
      });
    return () => {
      disposed = true;
    };
  }, [project.id, kind]);
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
  }, [ready, libraries, kind, search]);
  const total = selection.length * Number(count || 0);
  const offline =
    kind === "montage" &&
    assets.some((asset) => selection.includes(asset.id) && !asset.available);
  const backgroundReady = libraries.some(
    (library) => library.role === "background" && library.active_count > 0,
  );
  const configured = visionReady && (kind !== "montage" || backgroundReady);
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
    setSubmitting(true);
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
    }
  }
  return (
    <Stack gap="md">
      {error && <Alert color="red">{error}</Alert>}
      {ready && !configured && (
        <Alert color="yellow">
          {!visionReady
            ? "先在设置中选择 Codex 模型。"
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
      {kind === "montage" ? (
        <>
          <MultiSelect
            label="混剪视频"
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
        </>
      ) : (
        <>
          <MultiSelect
            label="处理文件夹"
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
            递归处理所选文件夹中的全部 MP4。结果保存到各输入目录同级的“
            {kind === "cover" ? "字幕查重" : "去重"}”目录，保留源视频。
          </Text>
        </>
      )}
      <TextInput
        label="成片输出目录"
        placeholder={
          kind === "montage" ? "选择成片保存位置" : "留空时保存到源目录同级"
        }
        value={output}
        onChange={(event) => setOutput(event.currentTarget.value)}
        rightSection={
          <Button
            variant="subtle"
            size="compact-xs"
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
      <Group justify="space-between" gap="sm" mt="sm">
        <Text size="sm" c="dimmed">
          {kind === "montage"
            ? `已选 ${selection.length} 个视频 · 共生成 ${total} 支`
            : `已选 ${folders.length} 个文件夹`}
        </Text>
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
