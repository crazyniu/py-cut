import { useEffect, useState } from "react";
import {
  Alert,
  Box,
  CopyButton,
  Button,
  Group,
  Loader,
  Modal,
  Pagination,
  Paper,
  Progress,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import {
  IconCheck,
  IconClock,
  IconFolderOpen,
  IconMovie,
} from "@tabler/icons-react";
import {
  type Job,
  type Output,
  type TaskItem,
  terminal,
  openPath,
  rpc,
} from "./api";
import {
  Status,
  MediaCard,
  VideoThumbnail,
  PreviewDetails,
  report,
} from "./common";
const names: Record<string, string> = {
  queued: "等待处理",
  running: "正在处理",
  completed: "已完成",
  review: "待复查",
  failed: "失败",
  skipped: "已跳过",
  cancelled: "已取消",
  interrupted: "已中断",
  unprocessed: "未处理",
};
export default function TaskProgress({ job }: { job: Job }) {
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(1);
  const [preview, setPreview] = useState<Output>();
  const [directory, setDirectory] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  useEffect(() => {
    setPage(1);
    setDirectory(null);
  }, [filter, job.id]);
  const outputs = job.outputs || [];
  const items: TaskItem[] = job.items?.length
    ? job.items
    : outputs.map((output) => ({
        id: output.id,
        output_id: output.id,
        name: output.name,
        path: output.path,
        status: "completed",
      }));
  const filtered = items.filter(
    (item) => filter === "all" || item.status === filter,
  );
  const pages = Math.max(1, Math.ceil(filtered.length / 24));
  const shownPage = Math.min(page, pages);
  const processed =
    job.completed + job.failed + job.review + (job.skipped || 0);
  const active = items.filter((item) => item.status === "running");
  const directories = [
    ...new Set(
      outputs.map((output) => output.path.replace(/[\\/][^\\/]+$/, "")),
    ),
  ];
  const target =
    directory && directories.includes(directory) ? directory : directories[0];
  const hasError = !!job.error || job.failed > 0 || job.status === "failed";
  return (
    <Paper withBorder p="lg" mb="lg">
      <Stack gap="md">
        <Group gap="md" align="center">
          <Text fw={600}>视频处理进度</Text>
          <Group>
            <Status value={job.status} />
            {target && (
              <Button
                size="xs"
                variant="default"
                leftSection={<IconFolderOpen size={14} />}
                onClick={() => openPath(target).catch(report)}
              >
                打开本批目录
              </Button>
            )}
            {!terminal(job) && (
              <Button
                size="xs"
                color="red"
                variant="light"
                loading={stopping}
                disabled={job.status === "cancelling"}
                onClick={async () => {
                  setStopping(true);
                  try {
                    await rpc("jobs.cancel", { job_id: job.id });
                  } catch (error) {
                    report(error);
                  } finally {
                    setStopping(false);
                  }
                }}
              >
                停止处理
              </Button>
            )}
          </Group>
        </Group>
        {directories.length > 1 && (
          <Select
            aria-label="本批成片目录"
            w={280}
            value={target}
            onChange={setDirectory}
            allowDeselect={false}
            data={directories.map((value) => ({
              value,
              label: value.split(/[\\/]/).pop() || value,
            }))}
          />
        )}
        <div className="processing-journey" aria-label="整批处理状态">
          <div>
            <IconClock size={20} />
            <Text size="sm">等待处理</Text>
            <Text size="xs" c="dimmed">
              {items.filter((item) => item.status === "queued").length} 个
            </Text>
          </div>
          <span
            className={
              active.length ? "processing-link active" : "processing-link"
            }
          />
          <div>
            {!terminal(job) && job.status !== "queued" ? (
              <Loader size={20} />
            ) : (
              <IconMovie size={20} />
            )}
            <Text size="sm">正在处理</Text>
            <Text size="xs" c="dimmed">
              {active.length
                ? `${active.length} 个视频`
                : job.status === "running"
                  ? "正在准备素材"
                  : "—"}
            </Text>
          </div>
          <span
            className={
              active.length ? "processing-link active" : "processing-link"
            }
          />
          <div>
            <IconCheck size={20} />
            <Text size="sm">处理结果</Text>
            <Text size="xs" c="dimmed">
              完成 {job.completed} · 待复查 {job.review} · 失败 {job.failed}
            </Text>
          </div>
        </div>
        <Group justify="space-between">
          <Text size="sm">
            已处理 {processed} / {job.planned} 个视频
          </Text>
          {!!job.skipped && (
            <Text size="xs" c="dimmed">
              跳过 {job.skipped} 个已有结果
            </Text>
          )}
        </Group>
        <Progress
          value={job.planned ? (processed / job.planned) * 100 : 0}
          animated={!terminal(job)}
          aria-label="整批视频进度"
        />
        {hasError && (
          <Alert color="red" title="处理遇到问题">
            <Text size="sm">
              {job.error || "部分视频未能完成，请复制日志查看原因。"}
            </Text>
            <CopyButton
              value={[
                job.error || "",
                ...(job.logs || []).map((line) =>
                  typeof line === "string" ? line : JSON.stringify(line),
                ),
              ].join("\n")}
            >
              {({ copied, copy }) => (
                <Button
                  mt="sm"
                  size="xs"
                  variant="light"
                  color="red"
                  onClick={copy}
                >
                  {copied ? "已复制日志" : "复制错误日志"}
                </Button>
              )}
            </CopyButton>
          </Alert>
        )}
        <Group gap="md" align="center">
          <Text size="sm" fw={500}>
            本批视频 · {items.length} 个
          </Text>
          <Select
            aria-label="筛选处理状态"
            w={160}
            value={filter}
            allowDeselect={false}
            onChange={(value) => value && setFilter(value)}
            data={[
              { value: "all", label: "全部状态" },
              ...Object.entries(names).map(([value, label]) => ({
                value,
                label,
              })),
            ]}
          />
        </Group>
        <div className="asset-grid">
          {filtered.slice((shownPage - 1) * 24, shownPage * 24).map((item) => {
            const output = outputs.find((value) => value.id === item.output_id);
            const current = item.progress;
            const percentage =
              item.status === "completed" && output
                ? 100
                : item.status === "running" && current?.total
                  ? Math.min(
                      99,
                      Math.floor(
                        ((current.completed || 0) / current.total) * 100,
                      ),
                    )
                  : null;
            return (
              <MediaCard
                key={item.id}
                title={item.name}
                label={`${item.name} ${names[item.status] || item.status}`}
                status={item.status}
                disabled={!output || output.available === false}
                onOpen={() => output && setPreview(output)}
                thumbnail={
                  <VideoThumbnail
                    id={output?.id || item.id.split(":")[0]}
                    output={!!output}
                    available={output?.available !== false}
                  />
                }
                overlay={
                  item.status === "running" && (
                    <Box className="thumbnail-progress">
                      <Loader size={28} />
                      <Text fw={600}>
                        {current?.stage || "处理中"}
                        {percentage !== null ? ` ${percentage}%` : "…"}
                      </Text>
                    </Box>
                  )
                }
              >
                <Text size="xs">
                  {output ? "点击预览" : names[item.status] || item.status}
                </Text>
              </MediaCard>
            );
          })}
        </div>
        {!filtered.length && (
          <Text size="sm" c="dimmed">
            没有此状态的视频
          </Text>
        )}
        {pages > 1 && (
          <Group justify="center">
            <Pagination value={shownPage} onChange={setPage} total={pages} />
          </Group>
        )}
        <Modal
          opened={!!preview}
          onClose={() => setPreview(undefined)}
          title={preview?.name || "成片预览"}
          size={720}
          centered
        >
          {preview && (
            <PreviewDetails
              id={preview.id}
              path={preview.path}
              output
              available={preview.available !== false}
            />
          )}
        </Modal>
      </Stack>
    </Paper>
  );
}
