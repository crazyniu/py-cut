import { useEffect, useState, type ReactNode } from "react";
import {
  Alert,
  Box,
  CopyButton,
  Button,
  Group,
  Loader,
  Modal,
  Pagination,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { IconFolderOpen } from "@tabler/icons-react";
import {
  type Job,
  type Output,
  type TaskItem,
  terminal,
  openPath,
  rpc,
} from "./api";
import { MediaCard, VideoThumbnail, PreviewDetails, report } from "./common";
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
  deleted: "已删除",
};
export function TaskActions({ job }: { job: Job }) {
  const [directory, setDirectory] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const directories = [
    ...new Set(
      [...(job.batch_available && job.batch_dir ? [job.batch_dir] : []), ...(job.outputs || []).map((output) =>
        output.path.replace(/[\\/][^\\/]+$/, ""),
      )],
    ),
  ];
  const target =
    directory && directories.includes(directory) ? directory : directories[0];
  return (
    <Group gap="xs">
      {directories.length > 1 && (
        <Select
          aria-label="本批成片目录"
          w={220}
          value={target}
          onChange={setDirectory}
          allowDeselect={false}
          data={directories.map((value) => ({
            value,
            label: value.split(/[\\/]/).pop() || value,
          }))}
        />
      )}
      {target && (
        <Button
          variant="default"
          leftSection={<IconFolderOpen size={16} />}
          onClick={() => openPath(target).catch(report)}
        >
          打开本批目录
        </Button>
      )}
      {!terminal(job) && (
        <Button
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
  );
}
export default function TaskProgress({ job, actions }: { job: Job; actions?: ReactNode }) {
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(1);
  const [preview, setPreview] = useState<Output>();
  useEffect(() => {
    setPage(1);
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
  const hasError = !!job.error || job.failed > 0 || job.status === "failed";
  return (
    <Stack gap="md">
      <Group gap="xs" align="center" className="page-toolbar">
        {actions}
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

      {job.review > 0 && (
        <Alert color="yellow" title="有视频需要复查">
          待复查结果需要检查后再使用，请查看对应视频与处理提示。
        </Alert>
      )}
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
      <div className="asset-grid">
        {filtered.slice((shownPage - 1) * 24, shownPage * 24).map((item) => {
          const output = outputs.find((value) => value.id === item.output_id);
          return (
            <MediaCard
              key={item.id}
              title={output?.name || item.name}
              label={`${item.name} ${names[item.status] || item.status}`}
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
                item.status !== "completed" && (
                  <Box className="thumbnail-progress">
                    {item.status === "running" && <Loader size={28} />}
                    <Text fw={600}>{item.status === "running" ? "处理中" : names[item.status] || item.status}</Text>
                  </Box>
                )
              }
            />
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
  );
}
