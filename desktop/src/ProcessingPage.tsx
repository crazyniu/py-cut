import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Group,
  Modal,
  Progress,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { IconArrowLeft, IconPlus, IconRefresh } from "@tabler/icons-react";
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
import {
  Empty,
  LoadingPage,
  PageHeading,
  MediaCard,
  VideoThumbnail,
} from "./common";
import TaskPage from "./TaskPage";
import TaskProgress from "./TaskProgress";

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
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("all");
  const [state, setState] = useState("all");
  const [selectedId, setSelectedId] = useState<string>();
  const [create, setCreate] = useState(false);
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
  return (
    <>
      <PageHeading
        title="视频处理"
        description="统一查看混剪、字幕擦除和视频去重任务。"
      />
      <Group gap="xs" mb="md" className="page-toolbar">
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
        <Button
          ml="auto"
          leftSection={<IconPlus size={16} />}
          onClick={() => setCreate(true)}
        >
          新建任务
        </Button>
      </Group>
      {error && (
        <Alert color="red" mb="md">
          {error}
        </Alert>
      )}
      {!loaded ? (
        <LoadingPage />
      ) : selected ? (
        <>
          <Group gap="sm" mb="md">
            <Text fw={600}>{kinds[selected.kind]}</Text>
            <Text size="sm" c="dimmed">
              {dateTime(selected.created_at)}
            </Text>
          </Group>
          <TaskProgress key={selected.id} job={selected} />
        </>
      ) : !visible.length ? (
        <Empty
          title={jobs.length ? "暂无符合条件的任务" : "还没有处理任务"}
          description="新建任务后，可在这里同时查看不同类型的处理状态。"
        />
      ) : (
        <div className="asset-grid">
          {visible.map((job) => {
            const processed =
              job.completed + job.failed + job.review + (job.skipped || 0);
            const output = job.outputs?.find(
              (item) => item.available !== false,
            );
            return (
              <MediaCard
                key={job.id}
                title={kinds[job.kind]}
                label={`查看${kinds[job.kind]}任务 ${dateTime(job.created_at)}`}
                status={job.status}
                onOpen={() => setSelectedId(job.id)}
                thumbnail={
                  <VideoThumbnail
                    id={output?.id || job.items?.[0]?.id.split(":")[0]}
                    output={!!output}
                  />
                }
              >
                <Text size="xs">{dateTime(job.created_at)}</Text>
                <Text size="xs">
                  已处理 {processed} / {job.planned} 个视频
                </Text>
                {!terminal(job) && (
                  <Progress
                    size={4}
                    mt={5}
                    value={job.planned ? (processed / job.planned) * 100 : 0}
                    animated={job.status === "running"}
                    aria-label={`${kinds[job.kind]}整批进度`}
                  />
                )}
              </MediaCard>
            );
          })}
        </div>
      )}
      <Modal
        opened={create}
        onClose={() => setCreate(false)}
        title="新建视频处理任务"
        size={560}
        centered
        closeOnClickOutside={false}
        className="task-form"
      >
        <Select
          label="处理类型"
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
