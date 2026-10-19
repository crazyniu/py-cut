import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Group,
  Modal,
  Pagination,
  Select,
  Text,
  TextInput,
} from "@mantine/core";
import {
  IconArrowLeft,
  IconFolderOpen,
  IconRefresh,
  IconSearch,
} from "@tabler/icons-react";
import {
  kinds,
  dateTime,
  message,
  openPath,
  rpc,
  type Job,
  type Output,
  type Project,
} from "./api";
import {
  Empty,
  LoadingPage,
  PreviewDetails,
  PageHeading,
  report,
  MediaCard,
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
  const [loaded, setLoaded] = useState(false);
  const [selectedId, setSelectedId] = useState<string>();
  const [preview, setPreview] = useState<Output>();
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [directory, setDirectory] = useState<string | null>(null);
  const [page, setPage] = useState(1);
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
  useEffect(() => {
    setPage(1);
    setDirectory(null);
  }, [selectedId, search, filter]);
  const detail = jobs.find((job) => job.id === selectedId);
  const query = search.trim().toLocaleLowerCase();
  const batches = jobs.filter(
    (job) =>
      job.outputs?.length &&
      (filter === "all" || job.kind === filter) &&
      (!query ||
        job.outputs.some((output) =>
          output.name.toLocaleLowerCase().includes(query),
        )),
  );
  const outputs = (detail?.outputs || []).filter(
    (output) => !query || output.name.toLocaleLowerCase().includes(query),
  );
  const directories = [
    ...new Set(
      (detail?.outputs || []).map((output) =>
        output.path.replace(/[\\/][^\\/]+$/, ""),
      ),
    ),
  ];
  const target =
    directory && directories.includes(directory) ? directory : directories[0];
  const count = selectedId ? outputs.length : batches.length;
  const pages = Math.max(1, Math.ceil(count / 24));
  const shownPage = Math.min(page, pages);
  function openBatch(job: Job) {
    setSelectedId(job.id);
    setSearch("");
  }
  return (
    <>
      <PageHeading
        title="成片库"
        description={
          detail
            ? `${kinds[detail.kind]} · ${dateTime(detail.created_at)}`
            : "按处理批次查看已生成的视频。"
        }
      />
      <Group gap="xs" mb="md" className="page-toolbar">
        {selectedId ? (
          <Button
            variant="default"
            leftSection={<IconArrowLeft size={16} />}
            onClick={() => {
              setSelectedId(undefined);
              setSearch("");
            }}
          >
            返回成片库
          </Button>
        ) : (
          <Select
            allowDeselect={false}
            w={150}
            aria-label="筛选成片类型"
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
        )}
        <TextInput
          aria-label="搜索成片"
          placeholder="搜索成片名称"
          w={180}
          value={search}
          onChange={(event) => setSearch(event.currentTarget.value)}
          leftSection={<IconSearch size={16} />}
        />
        {directories.length > 1 && (
          <Select
            aria-label="成片所在目录"
            w={220}
            value={target}
            allowDeselect={false}
            onChange={setDirectory}
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
        <Button
          variant="default"
          leftSection={<IconRefresh size={16} />}
          onClick={load}
        >
          刷新
        </Button>
        <Text size="sm" c="dimmed">
          {selectedId ? `${count} 支成片` : `${count} 个批次`}
        </Text>
      </Group>
      {error && (
        <Alert color="red" mb="md">
          {error}
        </Alert>
      )}
      {!loaded ? (
        <LoadingPage />
      ) : !count ? (
        <Empty
          title={query ? "未找到成片" : "暂无成片"}
          description={
            query ? "换个名称搜索。" : "视频处理生成的结果会自动出现在这里。"
          }
        />
      ) : selectedId ? (
        <div className="asset-grid">
          {outputs.slice((shownPage - 1) * 24, shownPage * 24).map((output) => (
            <MediaCard
              key={output.id}
              title={output.name}
              label={`预览${output.name}`}
              onOpen={() => setPreview(output)}
              thumbnail={
                <VideoThumbnail
                  id={output.id}
                  output
                  available={output.available !== false}
                />
              }
            >
              <Text size="xs">{dateTime(output.created_at)}</Text>
              {output.available === false && <Text size="xs">文件已移走</Text>}
            </MediaCard>
          ))}
        </div>
      ) : (
        <div className="task-grid">
          {batches.slice((shownPage - 1) * 24, shownPage * 24).map((job) => {
            const output =
              job.outputs?.find((item) => item.available !== false) ||
              job.outputs?.[0];
            return (
              <MediaCard
                key={job.id}
                title={`${kinds[job.kind]} · ${job.outputs?.length} 支成片`}
                label={`查看${kinds[job.kind]}成片 ${dateTime(job.created_at)}`}
                onOpen={() => openBatch(job)}
                thumbnail={
                  <VideoThumbnail
                    id={output?.id}
                    output
                    available={output?.available !== false}
                  />
                }
              >
                <Text size="xs">{dateTime(job.created_at)}</Text>
              </MediaCard>
            );
          })}
        </div>
      )}
      {pages > 1 && (
        <Group justify="center" mt="md">
          <Pagination value={shownPage} onChange={setPage} total={pages} />
        </Group>
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
