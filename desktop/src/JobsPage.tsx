import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  ActionIcon,
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
  IconTrash,
  IconFolderOpen,
  IconFolder,
  IconRefresh,
  IconSearch,
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
import {
  Empty,
  LoadingPage,
  PreviewDetails,
  PageHeading,
  report,
  MediaCard,
  VideoThumbnail,
} from "./common";

import TrashDialog, { type TrashTarget } from "./TrashDialog";
export default function JobsPage({
  project,
  revision,
}: {
  project: Project;
  revision: number;
}) {
  const [trash, setTrash] = useState<TrashTarget | null>(null);
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
  function folderName(job: Job) {
    const paths = [...new Set((job.outputs || []).map((output) => output.path.replace(/[\\/][^\\/]+$/, "")))];
    return (job.batch_dir || (paths.length === 1 ? paths[0] : "")).split(/[\\/]/).pop() || `${kinds[job.kind]} · ${dateTime(job.created_at)}`;
  }
  const query = search.trim().toLocaleLowerCase();
  const batches = jobs.filter(
    (job) =>
      (job.outputs?.length || job.batch_available) &&
      (filter === "all" || job.kind === filter) &&
      (!query || folderName(job).toLocaleLowerCase().includes(query) ||
        job.outputs?.some((output) =>
          output.name.toLocaleLowerCase().includes(query),
        )),
  );
  const outputs = (detail?.outputs || []).filter(
    (output) => !query || output.name.toLocaleLowerCase().includes(query),
  );
  const directories = [
    ...new Set(
      [...(detail?.batch_available && detail.batch_dir ? [detail.batch_dir] : []), ...(detail?.outputs || []).map((output) =>
        output.path.replace(/[\\/][^\\/]+$/, ""),
      )],
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
        title={detail ? folderName(detail) : "成片库"}
        description={
          detail
            ? `${kinds[detail.kind]} · ${dateTime(detail.created_at)}`
            : "每个任务一个文件夹，打开查看本次生成的视频。"
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
          placeholder={selectedId ? "搜索成片名称" : "搜索文件夹或成片"}
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
        {detail && <Button color="red" variant="subtle" leftSection={<IconTrash size={16} />} disabled={!terminal(detail)}
          onClick={() => setTrash({ kind: "batch", project_id: project.id, job_id: detail.id, label: folderName(detail) })}>删除成片文件夹</Button>}
        <Text size="sm" c="dimmed">
          {selectedId ? `${count} 支成片` : `${count} 个文件夹`}
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
              selection={<ActionIcon variant="default" size={28} color="red" aria-label={`删除成片 ${output.name}`}
                disabled={!!detail && !terminal(detail)}
                onClick={() => setTrash({ kind: "outputs", project_id: project.id, ids: [output.id], label: output.name })}><IconTrash size={16} /></ActionIcon>}
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
        <div className="batch-grid">
          {batches.slice((shownPage - 1) * 24, shownPage * 24).map((job) => (
            <div className="batch-folder-shell" key={job.id}><button className="batch-folder" onClick={() => openBatch(job)} aria-label={`打开文件夹 ${folderName(job)}`}>
              <IconFolder size={46} stroke={1.4} aria-hidden="true" />
              <Text component="span" size="sm" fw={600} lineClamp={2}>{folderName(job)}</Text>
              <Text component="span" size="xs" c="dimmed">{kinds[job.kind]} · {job.outputs?.length} 支成片</Text>
              <Text component="span" size="xs" c="dimmed">{dateTime(job.created_at)}</Text>
            </button>
            <ActionIcon className="batch-trash" variant="subtle" color="red" aria-label={`删除文件夹 ${folderName(job)}`} disabled={!terminal(job)}
              onClick={() => setTrash({ kind: "batch", project_id: project.id, job_id: job.id, label: folderName(job) })}><IconTrash size={17} /></ActionIcon>
            </div>
          ))}
        </div>
      )}
      {pages > 1 && (
        <Group justify="center" mt="md">
          <Pagination value={shownPage} onChange={setPage} total={pages} />
        </Group>
      )}
      <TrashDialog target={trash} onClose={() => setTrash(null)} onChanged={() => {
        setPreview(undefined); if (trash?.kind === "batch") setSelectedId(undefined); load();
      }} />
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
