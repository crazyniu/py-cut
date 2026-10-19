import { useEffect, useMemo, useRef, useState } from "react";
import { useDebouncedCallback } from "@mantine/hooks";
import {
  Alert,
  Badge,
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
  Progress,
  Select,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import {
  IconArrowRight,
  IconFolderOpen,
  IconPlayerPlay,
  IconRefresh,
} from "@tabler/icons-react";
import {
  chooseDirectory,
  chooseFile,
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
import { PageHeading, report, Status } from "./common";
import AdvancedOptions from "./AdvancedOptions";
import Workflow from "./Workflow";
export default function TaskPage({
  project,
  kind,
  revision,
  onJobs,
}: {
  project: Project;
  kind: JobKind;
  revision: number;
  onJobs: () => void;
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
  const [options, setOptions] = useState<Record<string, unknown>>({});
  const [subtitles, setSubtitles] = useState(true);
  const [audio, setAudio] = useState(true);
  const [selectedNode, setSelectedNode] = useState("reference");
  const [reset, setReset] = useState(0);
  const [positions, setPositions] = useState<
    Record<string, { x: number; y: number }>
  >({});
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
  const [confirm, setConfirm] = useState(false);
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
          ? saved.selection.filter((value): value is string => typeof value === "string") : [];
        const restored = await Promise.all(selected.map((asset_id) =>
          rpc<Asset>("assets.detail", { asset_id, project_id: project.id }).catch(() => null)));
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
        if (typeof saved.out_dir === "string" && (kind !== "montage" || saved.out_dir.trim()))
          setOutput(saved.out_dir);
        if (saved.options && typeof saved.options === "object")
          setOptions(saved.options as Record<string, unknown>);
        if (typeof saved.subtitles === "boolean") setSubtitles(saved.subtitles);
        if (typeof saved.audio === "boolean") setAudio(saved.audio);
        if (typeof saved.selected_node === "string")
          setSelectedNode(saved.selected_node);
        if (saved.positions && typeof saved.positions === "object")
          setPositions(
            saved.positions as Record<string, { x: number; y: number }>,
          );
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
      options,
      subtitles,
      audio,
      selected_node: selectedNode,
      positions,
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
    options,
    subtitles,
    audio,
    selectedNode,
    positions,
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
        label: `${item.name} · ${item.active_count} 个`,
      }));
  const total = selection.length * Number(count || 0);
  const invalidCount =
    !Number.isInteger(Number(count)) || Number(count) < 1 || Number(count) > 99;
  const canSubmit =
    ready &&
    selection.length > 0 &&
    (kind !== "montage" ||
      Boolean(background && output.trim() && !invalidCount));
  const selectedAssets = assets.filter((item) => selection.includes(item.id));
  const offline = selectedAssets.filter((item) => !item.available);
  const enabled = useMemo(
    () => ({ pip: !!pip, intro: !!intro, sticker: !!sticker }),
    [pip, intro, sticker],
  );
  const counts = useMemo(
    () => ({
      reference: selection.length,
      background: libraries.find((item) => item.id === background)
        ?.active_count,
      pip: libraries.find((item) => item.id === pip)?.active_count,
      intro: libraries.find((item) => item.id === intro)?.active_count,
      sticker: libraries.find((item) => item.id === sticker)?.active_count,
    }),
    [selection.length, libraries, background, pip, intro, sticker],
  );
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
          ...options,
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
      setConfirm(false);
    } catch (error) {
      report(error);
    } finally {
      setSubmitting(false);
    }
  }
  const sourceControl = (
    <>
      <Select
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
          label="每个话术生成数量"
          description="同一数量应用于全部选中话术"
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
  return (
    <>
      <PageHeading
        title={kind === "montage" ? "混剪工作台" : kinds[kind]}
        description={
          kind === "montage"
            ? "选择话术与素材库，确认数量后生成成片。流程节点可拖动，处理顺序固定。"
            : kind === "cover"
              ? "使用本地 Agent 检测字幕，按现有毛玻璃覆盖流程处理并复检。"
              : "复用现有去重处理与质量自检，单独保存结果。"
        }
        actions={
          kind === "montage" ? (
            <Button
              size="xs"
              variant="default"
              leftSection={<IconRefresh size={14} />}
              onClick={() => {
                setPositions({});
                setReset((value) => value + 1);
              }}
            >
              整理画布
            </Button>
          ) : undefined
        }
      />
      {error && (
        <Alert color="red" mb="md" withCloseButton onClose={() => setError("")}>
          {error}
        </Alert>
      )}
      {job && (
        <Paper withBorder p="md" mb="md">
          <Group justify="space-between" mb="sm">
            <Group gap="sm">
              <Text size="sm" fw={600}>
                {kinds[job.kind]}任务
              </Text>
              <Status value={job.status} />
            </Group>
            <Button
              variant="subtle"
              size="xs"
              rightSection={<IconArrowRight size={14} />}
              onClick={onJobs}
            >
              任务详情与成片
            </Button>
          </Group>
          <Group justify="space-between" mb={6}>
            <Text size="sm">
              已完成 {job.completed} / {job.planned}{" "}
              {kind === "montage" ? "支成片" : "个视频"}
              {job.failed > 0 ? ` · 失败 ${job.failed}` : ""}
              {job.review > 0 ? ` · 待复查 ${job.review}` : ""}
            </Text>
            <Text size="xs" c="dimmed">
              {job.progress?.stage || job.stage || ""}
            </Text>
          </Group>
          <Progress
            value={job.planned ? (job.completed / job.planned) * 100 : 0}
            color={job.status === "failed" ? "red" : "cyan"}
          />
          {job.error && (
            <Text size="sm" c="red" mt="sm">
              {job.error}
            </Text>
          )}
        </Paper>
      )}
      {kind === "montage" ? (
        <div className={`workbench ${job ? "has-job" : ""}`}>
          <Box className="workflow-column">
            <Workflow
              job={job}
              selected={selectedNode}
              onSelect={setSelectedNode}
              enabled={enabled}
              counts={counts}
              reset={reset}
              positions={positions}
              onPositions={setPositions}
            />
            <Paper withBorder p="md" className="workflow-summary">
              <Group justify="space-between">
                <Box>
                  <Text size="sm" fw={600}>
                    {selection.length} 个话术 × {Number(count) || 0} 支
                  </Text>
                  <Text size="xs" c="dimmed" mt={3}>
                    本批计划生成 {total} 支成片
                  </Text>
                </Box>
                <Badge variant="light" color="gray">
                  9:16
                </Badge>
              </Group>
            </Paper>
          </Box>
          <Paper withBorder p="md" className="inspector">
            <Stack gap="md">
              <Select label="任务参数" aria-label="切换参数区域" value={selectedNode}
                allowDeselect={false} onChange={(value) => value && setSelectedNode(value)}
                data={Object.entries({reference: "话术与数量", background: "背景素材", pip: "画中画设置",
                  compose: "正文合成", intro: "前贴设置", sticker: "贴纸设置", output: "成片输出"})
                  .map(([value, label]) => ({value, label}))} />
              <Divider />
              {selectedNode === "reference" && sourceControl}
              {selectedNode === "background" && (
                <>
                  <Select
                    label="背景切片素材库"
                    data={libraryData("background")}
                    value={background}
                    onChange={setBackground}
                    placeholder="请选择背景素材库"
                  />
                  <Text size="xs" c="dimmed">
                    每支成片独立随机选片，使用次数仅作统计。
                  </Text>
                </>
              )}
              {selectedNode === "pip" && (
                <>
                  <Select
                    label="画中画素材库"
                    clearable
                    placeholder="跳过画中画"
                    data={libraryData("pip")}
                    value={pip}
                    onChange={setPip}
                  />
                  <Text size="xs" c="dimmed">
                    仅使用当前库内有效素材，保留画中画自身字幕。
                  </Text>
                </>
              )}
              {selectedNode === "intro" && (
                <>
                  <Select
                    label="前贴素材库"
                    clearable
                    placeholder="跳过前贴"
                    data={libraryData("intro")}
                    value={intro}
                    onChange={setIntro}
                  />
                  <Text size="xs" c="dimmed">
                    随机选取一个完整视频，在正文字幕与原声完成后拼接。
                  </Text>
                </>
              )}
              {selectedNode === "sticker" && (
                <Select
                  label="贴纸素材库"
                  clearable
                  placeholder="不叠加贴纸"
                  data={libraryData("sticker")}
                  value={sticker}
                  onChange={setSticker}
                />
              )}{" "}
              {selectedNode === "compose" && (
                <>
                  <Checkbox
                    label="烧录话术字幕"
                    checked={subtitles}
                    onChange={(event) =>
                      setSubtitles(event.currentTarget.checked)
                    }
                  />
                  {subtitles && (
                    <>
                      <Select
                        label="字幕来源"
                        value={
                          options.srt && options.srt !== "auto"
                            ? "custom"
                            : "auto"
                        }
                        data={[
                          { value: "auto", label: "自动使用关联同名 SRT" },
                          { value: "custom", label: "指定 SRT 文件" },
                        ]}
                        onChange={async (value) => {
                          if (value === "auto")
                            setOptions((current) => ({
                              ...current,
                              srt: "auto",
                            }));
                          else {
                            try {
                              const path = await chooseFile(
                                "选择 SRT 字幕文件",
                                ["srt"],
                              );
                              if (path)
                                setOptions((current) => ({
                                  ...current,
                                  srt: path,
                                }));
                            } catch (error) {
                              report(error);
                            }
                          }
                        }}
                      />
                      {options.srt && options.srt !== "auto" ? (
                        <Text
                          size="xs"
                          c="dimmed"
                          style={{ overflowWrap: "anywhere" }}
                        >
                          全部已选话术使用此字幕：{String(options.srt)}
                        </Text>
                      ) : null}
                    </>
                  )}
                  <Checkbox
                    label="保留话术原声"
                    checked={audio}
                    onChange={(event) => setAudio(event.currentTarget.checked)}
                  />
                  <Text size="xs" c="dimmed">
                    画中画与字幕可能在同一次编码中完成；进度按实际处理事件显示。
                  </Text>
                </>
              )}
              {selectedNode === "output" && outputControl}
              <Divider />
              <AdvancedOptions
                kind={kind}
                values={options}
                onChange={setOptions}
              />
              <Box mt="auto" className="task-submit">
                <Text size="xs" c="dimmed" mb="sm">
                  {!source
                    ? "先添加话术素材库"
                    : !selection.length
                      ? "选择话术视频后继续"
                      : !background
                        ? "请选择背景素材库"
                        : !output.trim()
                          ? "请选择输出目录"
                          : `将生成 ${total} 支成片`}
                </Text>
                <Button
                  fullWidth
                  leftSection={<IconPlayerPlay size={16} />}
                  disabled={!canSubmit || offline.length > 0}
                  onClick={() => setConfirm(true)}
                >
                  确认混剪任务
                </Button>
              </Box>
            </Stack>
          </Paper>
        </div>
      ) : (
        <Paper withBorder p="lg" maw={750}>
          <Stack gap="lg">
            {sourceControl}
            {outputControl}
            <AdvancedOptions
              kind={kind}
              values={options}
              onChange={setOptions}
            />
            <Group justify="flex-end">
              <Button
                leftSection={<IconPlayerPlay size={16} />}
                disabled={!canSubmit || offline.length > 0}
                onClick={() => setConfirm(true)}
              >
                确认{kinds[kind]}任务
              </Button>
            </Group>
          </Stack>
        </Paper>
      )}
      <Modal
        opened={confirm}
        onClose={() => setConfirm(false)}
        title={`确认${kinds[kind]}任务`}
        centered
      >
        <Stack>
          <Text size="sm">
            {kind === "montage"
              ? `${selection.length} 个话术，每个生成 ${count} 支，共 ${total} 支成片。`
              : `处理 ${selection.length} 个已选视频。`}
          </Text>
          <Text size="sm" c="dimmed">
            任务使用提交时的素材清单，后续素材库调整仅影响新任务。
          </Text>
          {kind === "montage" && (
            <Text size="sm">
              背景：{libraries.find((item) => item.id === background)?.name}
              ；画中画：
              {libraries.find((item) => item.id === pip)?.name || "跳过"}
              ；前贴：
              {libraries.find((item) => item.id === intro)?.name || "跳过"}
            </Text>
          )}
          <Text size="xs" style={{ overflowWrap: "anywhere" }}>
            输出：{output.trim() || "源目录同级默认目录"}
          </Text>
          <Button onClick={submit} loading={submitting}>
            加入任务队列
          </Button>
        </Stack>
      </Modal>
    </>
  );
}
