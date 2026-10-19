import { useEffect, useState } from "react";
import {
  Alert,
  Badge,
  Box,
  CopyButton,
  Button,
  Group,
  Loader,
  Pagination,
  Paper,
  Progress,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { IconCheck, IconClock, IconMovie } from "@tabler/icons-react";
import { type Job, type TaskItem, terminal } from "./api";
import { Status } from "./common";

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
  useEffect(() => {
    setPage(1);
  }, [filter, job.id]);
  const items = job.items || [];
  const filtered = items.filter(
    (item) => filter === "all" || item.status === filter,
  );
  const processed =
    job.completed + job.failed + job.review + (job.skipped || 0);
  const active = items.filter((item) => item.status === "running");
  function itemIcon(item: TaskItem) {
    return item.status === "running" ? (
      <Loader size={18} />
    ) : item.status === "completed" ? (
      <IconCheck size={19} />
    ) : item.status === "queued" ? (
      <IconClock size={19} />
    ) : (
      <IconMovie size={19} />
    );
  }
  return (
    <Paper withBorder p="lg" mb="lg">
      <Stack gap="md">
        <Group justify="space-between">
          <Text fw={600}>视频处理进度</Text>
          <Status value={job.status} />
        </Group>
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
        {active.length > 0 && (
          <Text size="sm" c="cyan" lineClamp={2} role="status">
            正在处理：{active.map((item) => item.name).join("、")}
          </Text>
        )}
        {job.error && (
          <Alert color="red" title="处理遇到问题">
            <Text size="sm">{job.error}</Text>
            <CopyButton
              value={[
                job.error,
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
        {items.length > 0 && (
          <>
            <Group justify="space-between">
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
                  ...Object.entries(names)
                    .filter(([status]) =>
                      items.some((item) => item.status === status),
                    )
                    .map(([value, label]) => ({ value, label })),
                ]}
              />
            </Group>
            <div className="processing-videos">
              {filtered.slice((page - 1) * 24, page * 24).map((item) => (
                <Box
                  key={item.id}
                  className={`processing-video ${item.status}`}
                >
                  <Group gap="sm" wrap="nowrap">
                    {itemIcon(item)}
                    <Text size="sm" lineClamp={2} style={{ flex: 1 }}>
                      {item.name}
                    </Text>
                  </Group>
                  <Badge
                    size="xs"
                    variant="light"
                    mt="sm"
                    color={
                      item.status === "completed"
                        ? "teal"
                        : item.status === "running"
                          ? "cyan"
                          : item.status === "failed"
                            ? "red"
                            : item.status === "review"
                              ? "yellow"
                              : "gray"
                    }
                  >
                    {names[item.status] || item.status}
                  </Badge>
                </Box>
              ))}
            </div>
            {!filtered.length && (
              <Text size="sm" c="dimmed">
                没有此状态的视频
              </Text>
            )}
            {filtered.length > 24 && (
              <Group justify="center">
                <Pagination
                  value={page}
                  onChange={setPage}
                  total={Math.ceil(filtered.length / 24)}
                />
              </Group>
            )}
          </>
        )}
      </Stack>
    </Paper>
  );
}
