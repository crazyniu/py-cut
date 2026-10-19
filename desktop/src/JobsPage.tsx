import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Group,
  Modal,
  Paper,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import {
  IconArrowLeft,
  IconFolderOpen,
  IconRefresh,
} from "@tabler/icons-react";
import {
  kinds,
  dateTime,
  message,
  openPath,
  rpc,
  terminal,
  type Job,
  type Output,
  type Project,
} from "./api";
import TaskProgress from "./TaskProgress";
import {
  Empty,
  PreviewDetails,
  PageHeading,
  report,
  Status,
  VideoThumbnail,
} from "./common";

export default function JobsPage({
  project,
  revision,
}: {
  project: Project;
  revision: number;
}) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [detail, setDetail] = useState<Job>();
  const [selectedId, setSelectedId] = useState<string>();
  const [preview, setPreview] = useState<Output>();
  const [directory, setDirectory] = useState<string | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    rpc<Job[]>("jobs.list", { project_id: project.id })
      .then((items) => {
        setJobs(items);
        setError("");
      })
      .catch((error) => setError(message(error)));
  }, [project.id]);
  useEffect(load, [load, revision]);
  useEffect(() => {
    if (!selectedId) return;
    let disposed = false;
    rpc<Job>("jobs.detail", { job_id: selectedId })
      .then((result) => {
        if (!disposed) setDetail(result);
      })
      .catch(report);
    return () => {
      disposed = true;
    };
  }, [selectedId, revision]);
  const outputs = detail?.outputs || [];
  const directories = [
    ...new Set(
      outputs.map((output) => output.path.replace(/[\\/][^\\/]+$/, "")),
    ),
  ];
  const targetDirectory =
    directory && directories.includes(directory) ? directory : directories[0];
  function openJob(job: Job) {
    setDetail(job);
    setSelectedId(job.id);
    setDirectory(null);
  }
  return (
    <>
      <PageHeading
        title={
          selectedId && detail
            ? `${kinds[detail.kind]} · ${dateTime(detail.created_at)}`
            : "任务与成片"
        }
        description={
          selectedId
            ? "本次任务生成的视频"
            : "每次任务独立保存，点击缩略图查看本批全部视频。"
        }
        actions={
          <Group>
            {selectedId && (
              <Button
                variant="default"
                leftSection={<IconArrowLeft size={14} />}
                onClick={() => setSelectedId(undefined)}
              >
                所有任务
              </Button>
            )}
            <Button
              variant="default"
              size="xs"
              leftSection={<IconRefresh size={14} />}
              onClick={load}
            >
              刷新
            </Button>
          </Group>
        }
      />
      {error && (
        <Alert color="red" mb="md">
          {error}
        </Alert>
      )}
      {selectedId && detail ? (
        <>
          <TaskProgress job={detail} />
          <Group mb="md" align="flex-end" justify="space-between">
            <Text fw={600}>本批成片 · {outputs.length} 支</Text>
            <Group>
              {directories.length > 1 && (
                <Select
                  aria-label="成片目录"
                  w={260}
                  data={directories.map((path) => ({
                    value: path,
                    label: path.split(/[\\/]/).pop() || path,
                  }))}
                  value={targetDirectory}
                  onChange={setDirectory}
                  allowDeselect={false}
                />
              )}
              {targetDirectory && (
                <Button
                  variant="default"
                  leftSection={<IconFolderOpen size={15} />}
                  onClick={() => openPath(targetDirectory).catch(report)}
                >
                  打开本批成片目录
                </Button>
              )}
              {!terminal(detail) && (
                <Button
                  color="red"
                  variant="light"
                  disabled={detail.status === "cancelling"}
                  onClick={async () => {
                    try {
                      setDetail(
                        await rpc<Job>("jobs.cancel", { job_id: detail.id }),
                      );
                      load();
                    } catch (error) {
                      report(error);
                    }
                  }}
                >
                  停止处理
                </Button>
              )}
            </Group>
          </Group>
          {!outputs.length ? (
            <Empty
              title={
                terminal(detail)
                  ? "本次任务没有生成成片"
                  : "成片将在处理完成后显示"
              }
            />
          ) : (
            <div className="asset-grid">
              {outputs.map((output) => (
                <Paper key={output.id} withBorder className="asset-card">
                  <button
                    className="asset-open"
                    onClick={() => setPreview(output)}
                    aria-label={`预览 ${output.name}`}
                    disabled={output.available === false}
                  >
                    <VideoThumbnail
                      id={output.id}
                      output
                      available={output.available !== false}
                    />
                    <Text p="xs" size="sm" lineClamp={1}>
                      {output.name}
                    </Text>
                  </button>
                </Paper>
              ))}
            </div>
          )}
        </>
      ) : !jobs.length ? (
        <Empty
          title="暂无任务"
          description="从混剪、字幕擦除或去重页面开始处理。"
        />
      ) : (
        <div className="task-grid">
          {jobs.map((job) => {
            const output = job.outputs?.find(
              (item) => item.available !== false,
            );
            const input = job.items?.[0];
            return (
              <Paper key={job.id} withBorder className="asset-card">
                <button
                  className="asset-open"
                  onClick={() => openJob(job)}
                  aria-label={`查看${kinds[job.kind]}任务 ${dateTime(job.created_at)}`}
                >
                  <VideoThumbnail
                    id={output?.id || input?.id.split(":")[0]}
                    output={!!output}
                  />
                  <Stack p="sm" gap="xs">
                    <Group justify="space-between">
                      <Text fw={600} size="sm">
                        {kinds[job.kind]}
                      </Text>
                      <Status value={job.status} />
                    </Group>
                    <Text size="xs" c="dimmed">
                      {dateTime(job.created_at)}
                    </Text>
                    <Text size="sm">
                      已生成 {job.outputs?.length || 0} / {job.planned} 支视频
                    </Text>
                  </Stack>
                </button>
              </Paper>
            );
          })}
        </div>
      )}
      <Modal
        opened={!!preview}
        onClose={() => setPreview(undefined)}
        title={preview?.name || "成片预览"}
        centered
        size={720}
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
    </>
  );
}
