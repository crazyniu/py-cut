import { useEffect, useRef, useState } from "react";
import { useDebouncedCallback } from "@mantine/hooks";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Divider,
  Group,
  Loader,
  Modal,
  MultiSelect,
  NumberInput,
  Paper,
  Select,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import {
  IconArrowRight,
  IconFolderOpen,
  IconPlayerPlay,
} from "@tabler/icons-react";
import {
  chooseDirectory,
  kinds,
  message,
  rpc,
  terminal,
  type Asset,
  type Job,
  type JobKind,
  type Library,
  type Project,
} from "./api";
import { PageHeading, report } from "./common";
import TaskProgress from "./TaskProgress";
export default function TaskPage({
  project,
  kind,
  revision,
  onJobs,
  onSettings,
}: {
  project: Project;
  kind: JobKind;
  revision: number;
  onJobs: () => void;
  onSettings: () => void;
}) {
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [source, setSource] = useState<string | null>(null);
  const [background, setBackground] = useState<string | null>(null);
  const [pip, setPip] = useState<string | null>(null);
  const [intro, setIntro] = useState<string | null>(null);
  const [sticker, setSticker] = useState<string | null>(null);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [selection, setSelection] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [assetTotal, setAssetTotal] = useState(0);
  const [count, setCount] = useState<number | string>(1);
  const [output, setOutput] = useState(
    kind === "montage" ? project.output_dir || "" : "",
  );
  const [subtitles, setSubtitles] = useState(true);
  const [audio, setAudio] = useState(true);
  const [visionReady, setVisionReady] = useState(kind !== "cover");
  useEffect(() => {
    if (kind === "cover")
      rpc<Record<string, string>>("settings.get")
        .then((value) => setVisionReady(Boolean(value.vision_model)))
        .catch(report);
  }, [kind]);
  const [ready, setReady] = useState(false);
  const skipFirstSave = useRef(true);
  const saveWorkbench = useDebouncedCallback(
    (values: Record<string, unknown>) => {
      void rpc("workbench.save", {
        project_id: project.id,
        kind,
        values,
      }).catch(report);
    },
    { delay: 400, flushOnUnmount: true },
  );
  const [configure, setConfigure] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [job, setJob] = useState<Job>();
  const [error, setError] = useState("");
  useEffect(() => {
    let disposed = false;
    Promise.all([
      rpc<Library[]>("libraries.list", { project_id: project.id }),
      rpc<Record<string, unknown>>("workbench.get", {
        project_id: project.id,
        kind,
      }),
    ])
      .then(async ([items, saved]) => {
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
        setAssets(restored.filter((item): item is Asset => item !== null));
        setSelection(selected);
        const existing = (value: unknown) =>
          typeof value === "string" && items.some((item) => item.id === value)
            ? value
            : null;
        setLibraries(items);
        setSource(
          existing(saved.library_id) ||
            items.find(
              (item) =>
                item.role === (kind === "montage" ? "reference" : "background"),
            )?.id ||
            null,
        );
        setBackground(
          existing(saved.background_library_id) ||
            items.find((item) => item.role === "background")?.id ||
            null,
        );
        setPip(existing(saved.pip_library_id));
        setIntro(existing(saved.intro_library_id));
        setSticker(existing(saved.sticker_library_id));
        if (typeof saved.fission === "number") setCount(saved.fission);
        if (
          typeof saved.out_dir === "string" &&
          (kind !== "montage" || saved.out_dir.trim())
        )
          setOutput(saved.out_dir);
        if (typeof saved.subtitles === "boolean") setSubtitles(saved.subtitles);
        if (typeof saved.audio === "boolean") setAudio(saved.audio);
        setReady(true);
      })
      .catch((error) => {
        if (!disposed) setError(message(error));
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
    saveWorkbench({
      library_id: source,
      selection,
      background_library_id: background,
      pip_library_id: pip,
      intro_library_id: intro,
      sticker_library_id: sticker,
      fission: Number(count) || 1,
      out_dir: output,
      subtitles,
      audio,
    });
  }, [
    ready,
    source,
    selection,
    background,
    pip,
    intro,
    sticker,
    count,
    output,
    subtitles,
    audio,
    saveWorkbench,
  ]);
  useEffect(() => {
    if (!source) return;
    let disposed = false;
    const timeout = window.setTimeout(() => {
      setLoading(true);
      rpc<{ items: Asset[]; total: number }>("assets.list", {
        library_id: source,
        query: search,
        removed: false,
        sort: "name",
        offset: 0,
        limit: 100,
        kind: "video",
      })
        .then((result) => {
          if (!disposed) {
            setAssets((previous) => [
              ...previous.filter(
                (item) =>
                  selection.includes(item.id) &&
                  !result.items.some((next) => next.id === item.id),
              ),
              ...result.items,
            ]);
            setAssetTotal(result.total);
          }
        })
        .catch((error) => {
          if (!disposed) setError(message(error));
        })
        .finally(() => {
          if (!disposed) setLoading(false);
        });
    }, 250);
    return () => {
      disposed = true;
      window.clearTimeout(timeout);
    };
  }, [source, search]);
  useEffect(() => {
    if (job && !terminal(job))
      rpc<Job>("jobs.detail", { job_id: job.id }).then(setJob).catch(report);
  }, [revision]);
  useEffect(() => {
    rpc<Job[]>("jobs.list", { project_id: project.id })
      .then((items) => setJob(items.find((item) => item.kind === kind)))
      .catch(report);
  }, [project.id, kind]);
  const libraryData = (role?: string) =>
    libraries
      .filter((item) => !role || item.role === role)
      .map((item) => ({
        value: item.id,
        label: item.name,
      }));
  const total = selection.length * Number(count || 0);
  const invalidCount =
    !Number.isInteger(Number(count)) || Number(count) < 1 || Number(count) > 99;
  const canSubmit =
    ready &&
    visionReady &&
    Boolean(source) &&
    selection.length > 0 &&
    (kind !== "montage" ||
      Boolean(background && output.trim() && !invalidCount));
  const selectedAssets = assets.filter((item) => selection.includes(item.id));
  const offline = selectedAssets.filter((item) => !item.available);
  async function submit() {
    setSubmitting(true);
    try {
      const result = await rpc<Job>("jobs.submit", {
        project_id: project.id,
        kind,
        library_id: source,
        asset_ids: kind !== "montage" ? selection : undefined,
        reference_ids: kind === "montage" ? selection : undefined,
        background_library_id: background,
        pip_library_id: pip,
        intro_library_id: intro,
        sticker_library_id: sticker,
        options: {
          ...(output.trim() ? { out_dir: output.trim() } : {}),
          ...(kind === "montage"
            ? {
                fission: Number(count),
                no_pip: !pip,
                no_sub: !subtitles,
                no_audio: !audio,
                no_sticker: !sticker,
                intro_enabled: !!intro,
              }
            : {}),
        },
      });
      setJob(result);
      setConfigure(false);
    } catch (error) {
      report(error);
    } finally {
      setSubmitting(false);
    }
  }
  const sourceControl = (
    <>
      <Select
        w={200}
        label={kind === "montage" ? "话术素材库" : "待处理素材库"}
        placeholder="请选择素材库"
        data={libraryData(kind === "montage" ? "reference" : undefined)}
        value={source}
        onChange={(value) => {
          setSource(value);
          setSelection([]);
          setAssets([]);
          setSearch("");
          setAssetTotal(0);
        }}
        searchable
        allowDeselect={false}
      />
      <MultiSelect
        w={280}
        label={kind === "montage" ? "话术视频" : "处理视频"}
        placeholder="搜索并多选视频"
        data={assets
          .filter(
            (item) =>
              item.kind === "video" &&
              (kind === "montage" || /\.mp4$/i.test(item.path)),
          )
          .map((item) => ({
            value: item.id,
            label: `${item.path.split(/[\\/]/).slice(-2).join(" / ")}${!item.available ? " · 离线" : ""}`,
            disabled: !item.available,
          }))}
        value={selection}
        onChange={setSelection}
        searchable
        searchValue={search}
        onSearchChange={setSearch}
        hidePickedOptions
        rightSection={loading ? <Loader size={14} /> : undefined}
        maxDropdownHeight={240}
      />
      {assetTotal > 100 && (
        <Text size="xs" c="dimmed">
          共 {assetTotal} 个素材，输入名称搜索；每次显示前 100 个。
        </Text>
      )}
      {offline.length > 0 && (
        <Text c="red" size="xs">
          所选素材已离线，请取消选择并刷新。
        </Text>
      )}
      {kind === "montage" && (
        <NumberInput
          w={160}
          label="每个话术生成数量"
          min={1}
          max={99}
          allowDecimal={false}
          value={count}
          onChange={setCount}
          clampBehavior="strict"
        />
      )}
    </>
  );
  const outputControl = (
    <>
      <TextInput
        w={360}
        label="成片输出目录"
        placeholder={
          kind === "montage" ? "请选择输出目录" : "留空时保存到源目录同级"
        }
        value={output}
        onChange={(event) => setOutput(event.currentTarget.value)}
        rightSection={
          <Button
            variant="subtle"
            size="compact-xs"
            aria-label="选择输出目录"
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
      {kind !== "montage" && (
        <Text size="xs" c="dimmed">
          默认另存到各输入目录同级的“{kind === "cover" ? "字幕查重" : "去重"}
          ”，保留源文件。
        </Text>
      )}
    </>
  );
  const taskForm = (
    <Stack gap="md">
      {kind === "cover" && !visionReady && (
        <Alert color="yellow">
          先选择字幕识别工具和模型。
          <Button variant="subtle" size="xs" onClick={onSettings}>
            前往设置
          </Button>
        </Alert>
      )}
      <Group align="flex-end" gap="md">
        {sourceControl}
      </Group>
      {kind === "montage" && (
        <>
          <Divider />
          <Group align="flex-end" gap="md">
            <Select
              w={170}
              label="背景素材库"
              placeholder="请选择背景素材库"
              data={libraryData("background")}
              value={background}
              onChange={setBackground}
              allowDeselect={false}
            />
            <Select
              w={170}
              label="画中画素材库"
              placeholder="不使用画中画"
              clearable
              data={libraryData("pip")}
              value={pip}
              onChange={setPip}
            />
            <Select
              w={170}
              label="前贴素材库"
              placeholder="不添加前贴"
              clearable
              data={libraryData("intro")}
              value={intro}
              onChange={setIntro}
            />
            <Select
              w={170}
              label="贴纸素材库"
              placeholder="不添加贴纸"
              clearable
              data={libraryData("sticker")}
              value={sticker}
              onChange={setSticker}
            />
          </Group>
          <Group gap="xl">
            <Checkbox
              label="添加话术字幕"
              checked={subtitles}
              onChange={(event) => setSubtitles(event.currentTarget.checked)}
            />
            <Checkbox
              label="保留话术原声"
              checked={audio}
              onChange={(event) => setAudio(event.currentTarget.checked)}
            />
          </Group>
        </>
      )}
      <Divider />
      <Group align="flex-end" gap="lg">
        {outputControl}
        <Text size="sm" c="dimmed">
          {kind === "montage"
            ? `已选 ${selection.length} 个话术，共生成 ${total} 支成片`
            : `已选 ${selection.length} 个视频`}
        </Text>
        <Button
          leftSection={<IconPlayerPlay size={16} />}
          disabled={!canSubmit || offline.length > 0}
          onClick={submit}
          loading={submitting}
        >
          开始{kinds[kind]}
        </Button>
      </Group>
    </Stack>
  );
  return (
    <>
      <PageHeading
        title={kinds[kind]}
        description={
          kind === "montage"
            ? "选择话术和素材，生成属于当前项目的成片。"
            : kind === "cover"
              ? "自动查找并遮盖视频字幕，完成后检查效果。"
              : "调整视频画面，另存处理结果。"
        }
      />
      {error && (
        <Alert color="red" mb="md" withCloseButton onClose={() => setError("")}>
          {error}
        </Alert>
      )}
      {job && (
        <>
          <Group justify="flex-end" mb="md">
            {!terminal(job) && (
              <Button
                variant="light"
                color="red"
                disabled={job.status === "cancelling"}
                onClick={async () => {
                  try {
                    setJob(await rpc<Job>("jobs.cancel", { job_id: job.id }));
                  } catch (error) {
                    report(error);
                  }
                }}
              >
                停止处理
              </Button>
            )}
            <Button variant="default" onClick={() => setConfigure(true)}>
              新建{kinds[kind]}
            </Button>
            <Button
              variant="subtle"
              rightSection={<IconArrowRight size={14} />}
              onClick={onJobs}
            >
              查看任务与成片
            </Button>
          </Group>
          <TaskProgress job={job} />
        </>
      )}
      {job ? (
        <Modal
          opened={configure}
          onClose={() => setConfigure(false)}
          title={`新建${kinds[kind]}`}
          size="xl"
          centered
        >
          {taskForm}
        </Modal>
      ) : (
        <Paper withBorder p="lg">
          {taskForm}
        </Paper>
      )}
    </>
  );
}
