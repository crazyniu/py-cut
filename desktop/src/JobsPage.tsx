import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Divider,
  Group,
  Modal,
  Paper,
  Progress,
  ScrollArea,
  SegmentedControl,
  SimpleGrid,
  Stack,
  Table,
  Text,
} from "@mantine/core";
import {
  IconFolderOpen,
  IconPlayerPlay,
  IconRefresh,
  IconSquare,
} from "@tabler/icons-react";
import {
  kinds,
  dateTime,
  message,
  openPath,
  reveal,
  rpc,
  terminal,
  useSummary,
  type Job,
  type Output,
  type Project,
} from "./api";
import { Empty, MediaPreview, PageHeading, report, Status } from "./common";
export default function JobsPage({
  project,
  revision,
}: {
  project: Project;
  revision: number;
}) {
  const [tab, setTab] = useState("jobs");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [outputs, setOutputs] = useState<Output[]>([]);
  const [detail, setDetail] = useState<Job>();
  const [selectedId, setSelectedId] = useState<string>();
  const [preview, setPreview] = useState<Output>();
  const [error, setError] = useState("");
  const [cancel, setCancel] = useState<Job>();
  const load = useCallback(() => {
    Promise.all([
      rpc<Job[]>("jobs.list", { project_id: project.id }),
      rpc<Output[]>("outputs.list", { project_id: project.id }),
    ])
      .then(([jobs, outputs]) => {
        setJobs(jobs);
        setOutputs(outputs);
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
  async function cancelJob() {
    if (!cancel) return;
    try {
      await rpc("jobs.cancel", { job_id: cancel.id });
      setCancel(undefined);
      load();
    } catch (error) {
      report(error);
    }
  }
  return (
    <>
      <PageHeading
        title="任务与成片"
        description="查看当前项目的任务队列、处理结果和已发布成片。"
        actions={
          <Button
            variant="default"
            size="xs"
            leftSection={<IconRefresh size={14} />}
            onClick={load}
          >
            刷新
          </Button>
        }
      />
      <Group justify="space-between" mb="lg">
        <SegmentedControl
          value={tab}
          onChange={setTab}
          data={[
            { value: "jobs", label: "处理任务" },
            { value: "outputs", label: "成片" },
          ]}
        />
        {project.output_dir && (
          <Button
            variant="subtle"
            size="xs"
            leftSection={<IconFolderOpen size={15} />}
            onClick={() => openPath(project.output_dir).catch(report)}
          >
            打开输出目录
          </Button>
        )}
      </Group>
      {error && (
        <Alert color="red" mb="md">
          {error}
        </Alert>
      )}
      {tab === "jobs" ? (
        !jobs.length ? (
          <Empty
            title="暂无任务"
            description="从混剪工作台、字幕擦除或去重页面提交任务。"
          />
        ) : (
          <Paper withBorder>
            <Table.ScrollContainer minWidth={720}>
              <Table highlightOnHover verticalSpacing="md">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>任务</Table.Th>
                    <Table.Th>状态</Table.Th>
                    <Table.Th>成片 / 结果</Table.Th>
                    <Table.Th>创建时间</Table.Th>
                    <Table.Th>操作</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {jobs.map((job) => (
                    <Table.Tr key={job.id}>
                      <Table.Td>
                        <Text fw={500} size="sm">
                          {kinds[job.kind]}
                        </Text>
                        <Text size="xs" c="dimmed">
                          {job.progress?.stage || job.stage || "等待调度"}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <Status value={job.status} />
                      </Table.Td>
                      <Table.Td>
                        <Text size="sm">
                          {job.completed} / {job.planned}
                        </Text>
                        {job.review > 0 && (
                          <Text c="yellow" size="xs">
                            {job.review} 个待复查
                          </Text>
                        )}
                        {job.failed > 0 && (
                          <Text c="red" size="xs">
                            {job.failed} 个失败
                          </Text>
                        )}
                      </Table.Td>
                      <Table.Td>
                        <Text size="xs" c="dimmed">
                          {dateTime(job.created_at)}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <Group gap={3}>
                          <Button
                            variant="subtle"
                            size="xs"
                            onClick={() => {
                              setSelectedId(job.id);
                              setDetail(job);
                            }}
                          >
                            查看
                          </Button>
                          {!terminal(job) && job.status !== "cancelling" && (
                            <Button
                              variant="subtle"
                              color="red"
                              size="xs"
                              onClick={() => setCancel(job)}
                            >
                              取消
                            </Button>
                          )}
                        </Group>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          </Paper>
        )
      ) : !outputs.length ? (
        <Empty
          title="暂无成片"
          description="只有成功发布的结果会显示在这里。失败或取消不会增加素材使用次数。"
        />
      ) : (
        <SimpleGrid cols={{ base: 2, md: 3, xl: 4 }} spacing="md">
          {outputs.map((output) => (
            <Paper key={output.id} withBorder p="md">
              <Box className="output-placeholder">
                <IconPlayerPlay size={32} stroke={1.4} />
                <Button
                  variant="light"
                  size="xs"
                  disabled={!output.available}
                  onClick={() => setPreview(output)}
                >
                  预览成片
                </Button>
              </Box>
              <Text mt="sm" size="sm" fw={500} lineClamp={2}>
                {output.name}
              </Text>
              <Text size="xs" c="dimmed" mt={5}>
                {dateTime(output.created_at)}
              </Text>
              <Button
                mt="sm"
                variant="subtle"
                size="xs"
                leftSection={<IconFolderOpen size={14} />}
                onClick={() => reveal(output.path).catch(report)}
              >
                在文件夹中显示
              </Button>
            </Paper>
          ))}
        </SimpleGrid>
      )}
      <Modal
        opened={!!selectedId}
        onClose={() => setSelectedId(undefined)}
        title="任务详情"
        size="lg"
        centered
      >
        {detail && (
          <Stack>
            <Group justify="space-between">
              <Text fw={600}>{kinds[detail.kind]}</Text>
              <Status value={detail.status} />
            </Group>
            <Text size="sm">
              完成 {detail.completed} / {detail.planned} · 待复查{" "}
              {detail.review} · 失败 {detail.failed}
            </Text>
            <Progress
              value={
                detail.planned ? (detail.completed / detail.planned) * 100 : 0
              }
            />
            {detail.error && <Alert color="red">{detail.error}</Alert>}
            {detail.progress && (
              <Text size="xs" c="dimmed">
                当前阶段：{detail.progress.stage} {detail.progress.completed}/
                {detail.progress.total} {detail.progress.detail}
              </Text>
            )}
            <Divider />
            <Text size="sm" fw={600}>
              处理日志
            </Text>
            <ScrollArea h={230}>
              <pre className="log-output">
                {detail.logs?.length
                  ? detail.logs
                      .map((item) =>
                        typeof item === "string" ? item : JSON.stringify(item),
                      )
                      .join("\n")
                  : "暂无日志"}
              </pre>
            </ScrollArea>
            <Divider />
            <Text size="sm" fw={600}>
              本任务成片
            </Text>
            {(
              detail.outputs ||
              outputs.filter((item) => item.job_id === detail.id)
            ).map((output) => (
              <Group justify="space-between" key={output.id} wrap="nowrap">
                <Text size="sm" lineClamp={1}>
                  {output.name}
                </Text>
                <Button
                  size="xs"
                  variant="subtle"
                  onClick={() => setPreview(output)}
                >
                  预览
                </Button>
              </Group>
            ))}
            {!terminal(detail) && detail.status !== "cancelling" && (
              <Button
                color="red"
                variant="light"
                leftSection={<IconSquare size={14} />}
                onClick={() => setCancel(detail)}
              >
                停止此任务
              </Button>
            )}
          </Stack>
        )}
      </Modal>
      <Modal
        opened={!!preview}
        onClose={() => setPreview(undefined)}
        title={preview?.name || "成片预览"}
        centered
        size="sm"
      >
        {preview && (
          <Stack>
            <MediaPreview
              method="media.output_preview"
              id={preview.id}
              output
            />
            <Button
              variant="default"
              onClick={() => reveal(preview.path).catch(report)}
            >
              在文件夹中显示
            </Button>
            <Divider />
            <Text size="sm" fw={600}>
              本片实际使用的素材
            </Text>
            <ScrollArea mah={240}>
              {preview.uses?.length ? (
                preview.uses.map((use, index) => (
                  <Box mb="md" key={index}>
                    <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                      {use.name || use.path}
                    </Text>
                    <Text size="xs" c="dimmed" mt={4}>
                      {useSummary(use)}
                    </Text>
                    {use.path && (
                      <Button
                        variant="subtle"
                        size="compact-xs"
                        mt={4}
                        onClick={() => reveal(use.path!).catch(report)}
                      >
                        定位素材
                      </Button>
                    )}
                  </Box>
                ))
              ) : (
                <Text size="xs" c="dimmed">
                  此处理结果没有混剪素材使用记录。
                </Text>
              )}
            </ScrollArea>
          </Stack>
        )}
      </Modal>
      <Modal
        opened={!!cancel}
        onClose={() => setCancel(undefined)}
        title="取消任务"
        centered
      >
        <Stack>
          <Text size="sm">
            {cancel?.status === "queued"
              ? "此任务将从队列中取消。"
              : "正在处理的任务将停止，并清理临时文件。已成功发布的成片会保留。"}
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setCancel(undefined)}>
              继续任务
            </Button>
            <Button color="red" onClick={cancelJob}>
              取消任务
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}
