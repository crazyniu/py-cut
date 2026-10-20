import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  ActionIcon,
  Button,
  Group,
  Modal,
  Pagination,
  Table,
  Progress,
  Select,
  Text,
} from "@mantine/core";
import { IconArrowLeft, IconPlus, IconRefresh, IconTrash } from "@tabler/icons-react";
import {
  dateTime,
  kinds,
  message,
  rpc,
  terminal,
  type Job,
  type JobKind,
  type Project,
} from "./api";
import { Empty, LoadingPage, PageHeading, Status } from "./common";
import TaskPage from "./TaskPage";
import TaskProgress, { TaskActions } from "./TaskProgress";

import TrashDialog, { type TrashTarget } from "./TrashDialog";
export default function ProcessingPage({
  project,
  revision,
  onSettings,
  onLibrary,
}: {
  project: Project;
  revision: number;
  onSettings: () => void;
  onLibrary: () => void;
}) {
  const [trash, setTrash] = useState<TrashTarget | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("all");
  const [state, setState] = useState("all");
  const [selectedId, setSelectedId] = useState<string>();
  const [create, setCreate] = useState(false);
  const [page, setPage] = useState(1);
  const [submitting, setSubmitting] = useState(false);
  const [kind, setKind] = useState<JobKind>("montage");
  const request = useRef(0);
  const load = useCallback(() => {
    const sequence = ++request.current;
    rpc<Job[]>("jobs.list", { project_id: project.id })
      .then((items) => {
        if (sequence === request.current) {
          setJobs(items);
          setError("");
        }
      })
      .catch((reason) => {
        if (sequence === request.current) setError(message(reason));
      })
      .finally(() => {
        if (sequence === request.current) setLoaded(true);
      });
  }, [project.id]);
  useEffect(load, [load, revision]);
  useEffect(
    () => () => {
      request.current++;
    },
    [],
  );
  const selected = jobs.find((job) => job.id === selectedId);
  const visible = jobs.filter(
    (job) =>
      (filter === "all" || job.kind === filter) &&
      (state === "all" ||
        (state === "active" ? !terminal(job) : terminal(job))),
  );
  const active = jobs.filter((job) => !terminal(job)).length;
  useEffect(() => setPage(1), [filter, state]);
  const pages = Math.max(1, Math.ceil(visible.length / 12));
  const shownPage = Math.min(page, pages);
  return (
    <>
      <PageHeading
        title={selected ? `${kinds[selected.kind]}任务` : "视频处理"}
        description={
          selected
            ? dateTime(selected.created_at)
            : "创建处理任务，查看进度与结果。"
        }
        actions={
          <Button
            leftSection={<IconPlus size={16} />}
            onClick={() => setCreate(true)}
          >
            新建任务
          </Button>
        }
      />
      {!selected && <Group gap="xs" mb="md" className="page-toolbar">
        {selectedId ? (
          <Button
            variant="default"
            leftSection={<IconArrowLeft size={16} />}
            onClick={() => setSelectedId(undefined)}
          >
            全部任务
          </Button>
        ) : (
          <>
            <Select
              allowDeselect={false}
              w={150}
              aria-label="筛选任务类型"
              value={filter}
              onChange={(value) => value && setFilter(value)}
              data={[
                { value: "all", label: "全部类型" },
                ...Object.entries(kinds).map(([value, label]) => ({
                  value,
                  label,
                })),
              ]}
            />
            <Select
              allowDeselect={false}
              w={140}
              aria-label="筛选任务状态"
              value={state}
              onChange={(value) => value && setState(value)}
              data={[
                { value: "all", label: "全部任务" },
                { value: "active", label: "处理中 / 排队" },
                { value: "finished", label: "已结束" },
              ]}
            />
          </>
        )}

        <Button
          variant="default"
          leftSection={<IconRefresh size={16} />}
          onClick={load}
        >
          刷新
        </Button>
        {!selectedId && (
          <Text size="sm" c="dimmed">
            {visible.length} 个任务{active ? ` · ${active} 个处理中或排队` : ""}
          </Text>
        )}
      </Group>}
      {error && (
        <Alert color="red" mb="md">
          {error}
        </Alert>
      )}
      {!loaded ? (
        <LoadingPage />
      ) : selected ? (
        <TaskProgress key={selected.id} job={selected} actions={<>
          <Button variant="default" leftSection={<IconArrowLeft size={16} />} onClick={() => setSelectedId(undefined)}>全部任务</Button>
          <TaskActions job={selected} />
          <Button variant="subtle" color="red" leftSection={<IconTrash size={16} />} disabled={selected.status === "running" || selected.status === "cancelling"}
            onClick={() => setTrash({ kind: "job", project_id: project.id, job_id: selected.id, label: `${kinds[selected.kind]} · ${dateTime(selected.created_at)}` })}>删除任务</Button>
          <Button variant="default" leftSection={<IconRefresh size={16} />} onClick={load}>刷新</Button>
        </>} />
      ) : !visible.length ? (
        <Empty
          title={jobs.length ? "暂无符合条件的任务" : "还没有处理任务"}
          description="新建任务后，可在这里同时查看不同类型的处理状态。"
        />
      ) : (
        <Table.ScrollContainer minWidth={620}>
          <Table verticalSpacing="md" highlightOnHover className="job-table">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>处理任务</Table.Th>
                <Table.Th>状态</Table.Th>
                <Table.Th>处理进度</Table.Th>
                <Table.Th>结果</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {visible
                .slice((shownPage - 1) * 12, shownPage * 12)
                .map((job) => {
                  const processed =
                    job.completed +
                    job.failed +
                    job.review +
                    (job.skipped || 0);
                  return (
                    <Table.Tr key={job.id}>
                      <Table.Td>
                        <button
                          className="text-button job-name"
                          onClick={() => setSelectedId(job.id)}
                        >
                          {kinds[job.kind]}
                        </button>
                        <Text size="xs" c="dimmed" mt={4}>
                          {dateTime(job.created_at)}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <Status value={job.status} />
                      </Table.Td>
                      <Table.Td>
                        <Text size="sm">
                          {processed} / {job.planned} 个视频
                        </Text>
                        {!terminal(job) && (
                          <Progress
                            size={4}
                            mt={8}
                            maw={180}
                            value={
                              job.planned ? (processed / job.planned) * 100 : 0
                            }
                            animated={job.status === "running"}
                            aria-label={`${kinds[job.kind]}整批进度`}
                          />
                        )}
                      </Table.Td>
                      <Table.Td>
                        <Text size="sm">
                          完成 {job.completed}
                          {job.review > 0 ? ` · 待复查 ${job.review}` : ""}
                          {job.failed > 0 ? ` · 失败 ${job.failed}` : ""}
                        </Text>
                        {!!job.skipped && (
                          <Text size="xs" c="dimmed">
                            跳过 {job.skipped}
                          </Text>
                        )}
                      </Table.Td>
                      <Table.Td ta="right">
                        <Group gap="xs" justify="flex-end" wrap="nowrap"><Button
                          variant="default"
                          size="xs"
                          onClick={() => setSelectedId(job.id)}
                        >
                          查看任务
                        </Button>
                        <ActionIcon variant="subtle" color="red" aria-label={`删除任务 ${kinds[job.kind]} ${dateTime(job.created_at)}`}
                          disabled={job.status === "running" || job.status === "cancelling"}
                          onClick={() => setTrash({ kind: "job", project_id: project.id, job_id: job.id, label: `${kinds[job.kind]} · ${dateTime(job.created_at)}` })}><IconTrash size={16} /></ActionIcon>
                        </Group>
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
      {!selected && pages > 1 && (
        <Group justify="center" mt="lg">
          <Pagination value={shownPage} onChange={setPage} total={pages} />
        </Group>
      )}
      <TrashDialog target={trash} onClose={() => setTrash(null)} onChanged={() => { setSelectedId(undefined); load(); }} />
      <Modal
        opened={create}
        onClose={() => !submitting && setCreate(false)}
        title="新建视频处理任务"
        size={680}
        centered
        closeOnClickOutside={false}
        closeOnEscape={!submitting}
        withCloseButton={!submitting}
        className="task-form"
      >
        <Select
          label="处理类型"
          disabled={submitting}
          mb="md"
          value={kind}
          allowDeselect={false}
          onChange={(value) => value && setKind(value as JobKind)}
          data={Object.entries(kinds).map(([value, label]) => ({
            value,
            label,
          }))}
        />
        <TaskPage
          key={kind}
          project={project}
          kind={kind}
          onSettings={onSettings}
          onLibrary={onLibrary}
          onCancel={() => setCreate(false)}
          onBusyChange={setSubmitting}
          onSubmitted={(job) => {
            setJobs((items) => [job, ...items]);
            setSelectedId(job.id);
            setCreate(false);
            load();
          }}
        />
      </Modal>
    </>
  );
}
