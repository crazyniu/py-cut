import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Group,
  Loader,
  Modal,
  Pagination,
  Paper,
  Select,
  SegmentedControl,
  Stack,
  Table,
  Text,
  TextInput,
} from "@mantine/core";
import {
  IconFolderOpen,
  IconLayoutGrid,
  IconList,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconTrash,
} from "@tabler/icons-react";
import { useDebouncedValue } from "@mantine/hooks";
import {
  AssetThumb,
  Empty,
  ErrorNotice,
  PreviewDetails,
  PageHeading,
  MediaCard,
  Status,
  report,
} from "./common";
import {
  chooseDirectories,
  duration,
  message,
  onEvent,
  roles,
  openPath,
  rpc,
  type Asset,
  type Library,
  type Project,
  type Use,
} from "./api";
export default function LibraryPage({ project }: { project: Project }) {
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [libraryId, setLibraryId] = useState<string | null>(null);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [debouncedQuery] = useDebouncedValue(query, 250);
  const [sort, setSort] = useState<string | null>("name");
  const [kind, setKind] = useState<string>("video");
  const [root, setRoot] = useState<string | null>(null);
  const [removed, setRemoved] = useState("active");
  const [layout, setLayout] = useState("grid");
  const [selected, setSelected] = useState<string[]>([]);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<(Asset & { uses: Use[] }) | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [scanned, setScanned] = useState<number | null>(null);
  const [revision, setRevision] = useState(0);
  const [directories, setDirectories] = useState(false);
  const [directoryError, setDirectoryError] = useState("");
  const [directoryNotice, setDirectoryNotice] = useState("");
  const library = libraries.find((item) => item.id === libraryId);
  const loadLibraries = useCallback(
    () =>
      rpc<Library[]>("libraries.list", { project_id: project.id })
        .then((items) => {
          setLibraries(items);
          setLibraryId((value) =>
            value && items.some((item) => item.id === value)
              ? value
              : items[0]?.id || null,
          );
        })
        .catch((error) => setError(message(error))),
    [project.id],
  );
  useEffect(() => {
    void loadLibraries();
  }, [loadLibraries]);
  useEffect(() => {
    let closed = false;
    let dispose: (() => void) | undefined;
    onEvent((event) => {
      if (event.event !== "library") return;
      const value = event.payload as {
        library_id: string;
        indexed: number;
        scanning: boolean;
      };
      if (value.library_id === libraryId)
        setScanned(value.scanning ? value.indexed : null);
    })
      .then((unlisten) => {
        if (closed) unlisten();
        else dispose = unlisten;
      })
      .catch(report);
    return () => {
      closed = true;
      dispose?.();
    };
  }, [libraryId]);
  useEffect(() => {
    setPage(1);
    setSelected([]);
    setDetailId(null);
    setRoot(null);
    setKind(
      libraries.find((item) => item.id === libraryId)?.role === "sticker"
        ? "image"
        : "video",
    );
  }, [libraryId]);
  useEffect(() => {
    setPage(1);
    setSelected([]);
  }, [debouncedQuery, sort, root, removed, kind]);
  const loadAssets = useCallback(() => {
    if (!libraryId) return;
    let current = true;
    setError("");
    rpc<{ items: Asset[]; total: number }>("assets.list", {
      library_id: libraryId,
      query: debouncedQuery,
      sort,
      kind,
      root,
      removed: removed === "removed",
      offset: (page - 1) * 24,
      limit: 24,
    })
      .then((result) => {
        if (current) {
          setAssets(result.items);
          setTotal(result.total);
        }
      })
      .catch((error) => {
        if (current) setError(message(error));
      });
    return () => {
      current = false;
    };
  }, [libraryId, debouncedQuery, sort, root, removed, page, kind]);
  useEffect(loadAssets, [loadAssets, revision]);
  useEffect(() => {
    setDetail(null);
    if (!detailId) return;
    let disposed = false;
    rpc<Asset & { uses: Use[] }>("assets.detail", {
      asset_id: detailId,
      project_id: project.id,
    })
      .then((result) => {
        if (!disposed) setDetail(result);
      })
      .catch(report);
    return () => {
      disposed = true;
    };
  }, [detailId, project.id, revision]);
  async function mutate(method: string, params: Record<string, unknown>) {
    setBusy(true);
    setDirectoryError("");
    setDirectoryNotice("");
    const showDirectoryError = (text: string) =>
      directories ? setDirectoryError(text) : report(new Error(text));
    try {
      const result = await rpc<{ errors?: { path: string; error: string }[] }>(
        method,
        params,
      );
      if (method === "libraries.add_roots") {
        const scan = await rpc<{ errors?: { path: string; error: string }[] }>(
          "libraries.scan",
          { library_id: params.library_id, roots: params.roots },
        );
        if (scan.errors?.length)
          showDirectoryError(
            scan.errors.map((item) => `${item.path}: ${item.error}`).join("\n"),
          );
      } else if (result?.errors?.length) {
        showDirectoryError(
          result.errors.map((item) => `${item.path}: ${item.error}`).join("\n"),
        );
      }
      if (method === "libraries.remove_root" && root === params.path)
        setRoot(null);
      await loadLibraries();
      setRevision((value) => value + 1);
    } catch (error) {
      if (method.startsWith("libraries.")) showDirectoryError(message(error));
      else report(error);
    } finally {
      setBusy(false);
      setScanned(null);
    }
  }
  async function addRoots() {
    if (!library) return;
    try {
      const chosen = await chooseDirectories();
      if (!chosen.length) return;
      const roots = chosen.filter((path) => !library.roots.includes(path));
      if (!roots.length) {
        setDirectoryError("");
        setDirectoryNotice("所选目录已添加到当前类型。");
        return;
      }
      await mutate("libraries.add_roots", { library_id: library.id, roots });
    } catch (error) {
      report(error);
    }
  }
  const toggle = (id: string) =>
    setSelected((ids) =>
      ids.includes(id) ? ids.filter((value) => value !== id) : [...ids, id],
    );
  return (
    <>
      <PageHeading
        title="素材库"
        description="关联本地目录，统一查找和管理素材。移除素材仅影响当前素材库。"
      />
      <Group mb="md" align="flex-end" gap="xs" className="page-toolbar">
        <Select
          aria-label="素材库"
          placeholder="请选择素材库"
          w={170}
          data={libraries.map((item) => ({
            value: item.id,
            label: `${roles[item.role]} · ${item.active_count} 个`,
          }))}
          value={libraryId}
          onChange={setLibraryId}
          allowDeselect={false}
          disabled={busy}
        />
        <Button
          variant="default"
          onClick={() => {
            setDirectoryError("");
            setDirectoryNotice("");
            setDirectories(true);
          }}
          disabled={!library}
        >
          管理目录
        </Button>
        <TextInput
          placeholder="搜索素材名称"
          aria-label="搜索素材"
          leftSection={<IconSearch size={16} />}
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
          w={160}
        />
        <Select
          allowDeselect={false}
          w={104}
          aria-label="素材类型"
          value={kind}
          onChange={(value) => value !== null && setKind(value)}
          data={[
            { value: "", label: "全部" },
            { value: "video", label: "视频" },
            { value: "image", label: "图片" },
            { value: "subtitle", label: "字幕" },
          ]}
        />
        <Select
          allowDeselect={false}
          w={120}
          aria-label="排序"
          data={[
            { value: "name", label: "名称" },
            { value: "usage", label: "使用次数" },
            { value: "duration", label: "时长" },
            { value: "size", label: "尺寸" },
          ]}
          value={sort || "name"}
          onChange={(value) => setSort(String(value))}
        />
        <Group gap={6} aria-label="显示方式">
          <Button
            variant={layout === "grid" ? "light" : "default"}
            w={36}
            p={0}
            aria-label="缩略图视图"
            aria-pressed={layout === "grid"}
            onClick={() => setLayout("grid")}
            styles={{
              root: { border: "1px solid var(--mantine-color-dark-4)" },
            }}
          >
            <IconLayoutGrid size={17} />
          </Button>
          <Button
            variant={layout === "list" ? "light" : "default"}
            w={36}
            p={0}
            aria-label="列表视图"
            aria-pressed={layout === "list"}
            onClick={() => setLayout("list")}
            styles={{
              root: { border: "1px solid var(--mantine-color-dark-4)" },
            }}
          >
            <IconList size={17} />
          </Button>
        </Group>
        <Button
          size="xs"
          variant="default"
          leftSection={<IconRefresh size={14} />}
          onClick={() =>
            library && mutate("libraries.scan", { library_id: library.id })
          }
          disabled={!library}
          loading={busy}
        >
          刷新目录
        </Button>
      </Group>
      <div>
        <Box className="library-main">
          {library && (
            <>
              {busy && (
                <Group gap="xs" py="sm" role="status" aria-live="polite">
                  <Loader size="xs" />
                  <Text size="sm" c="dimmed">
                    {scanned === null
                      ? "正在更新素材库…"
                      : `正在扫描，已读取 ${scanned} 个素材…`}
                  </Text>
                </Group>
              )}
              <Group gap="md" mb="md">
                <SegmentedControl
                  className="choice-group"
                  aria-label="素材状态"
                  value={removed}
                  onChange={(value) => value && setRemoved(value)}
                  data={[
                    { value: "active", label: "全部素材" },
                    { value: "removed", label: "已移除" },
                  ]}
                />
                <Select
                  clearable
                  placeholder="全部来源目录"
                  aria-label="筛选来源目录"
                  w={220}
                  data={library.roots.map((path) => ({
                    value: path,
                    label: path,
                  }))}
                  value={root}
                  onChange={setRoot}
                />
                {assets.length > 0 && (
                  <Group gap="md">
                    <Checkbox
                      size="xs"
                      label="选择本页"
                      checked={assets.every((a) => selected.includes(a.id))}
                      indeterminate={
                        assets.some((a) => selected.includes(a.id)) &&
                        !assets.every((a) => selected.includes(a.id))
                      }
                      onChange={(event) =>
                        setSelected(
                          event.currentTarget.checked
                            ? [
                                ...new Set([
                                  ...selected,
                                  ...assets.map((a) => a.id),
                                ]),
                              ]
                            : selected.filter(
                                (id) => !assets.some((a) => a.id === id),
                              ),
                        )
                      }
                    />
                    <Text size="xs" c="dimmed">
                      第 {(page - 1) * 24 + 1}–{Math.min(page * 24, total)}{" "}
                      项，共 {total} 项
                    </Text>
                  </Group>
                )}
              </Group>
              {selected.length > 0 && (
                <Paper withBorder p="xs" mb="md">
                  <Group justify="space-between">
                    <Text size="sm">已选 {selected.length} 个</Text>
                    <Group>
                      <Button
                        size="xs"
                        variant="subtle"
                        color="gray"
                        onClick={() => setSelected([])}
                      >
                        取消选择
                      </Button>
                      <Button
                        size="xs"
                        variant="light"
                        color={removed === "active" ? "red" : "cyan"}
                        leftSection={
                          removed === "active" ? (
                            <IconTrash size={14} />
                          ) : undefined
                        }
                        loading={busy}
                        onClick={async () => {
                          await mutate(
                            removed === "active"
                              ? "assets.remove"
                              : "assets.restore",
                            { library_id: library.id, asset_ids: selected },
                          );
                          setSelected([]);
                        }}
                      >
                        {removed === "active" ? "从当前库移除" : "恢复到素材库"}
                      </Button>
                    </Group>
                  </Group>
                </Paper>
              )}
              {error && (
                <ErrorNotice
                  error={error}
                  retry={() => setRevision((value) => value + 1)}
                />
              )}
              {!assets.length && !error && !busy ? (
                <Empty
                  title={
                    removed === "removed" ? "没有已移除素材" : "素材库为空"
                  }
                  description={
                    library.roots.length
                      ? "尝试刷新目录或调整搜索条件。"
                      : "添加一个或多个目录，素材将从原位置读取。"
                  }
                  action={
                    !library.roots.length ? (
                      <Button onClick={addRoots} variant="light">
                        添加目录
                      </Button>
                    ) : undefined
                  }
                />
              ) : layout === "grid" ? (
                <div className="asset-grid">
                  {assets.map((asset) => (
                    <MediaCard
                      key={asset.id}
                      active={detailId === asset.id}
                      title={asset.name}
                      label={`查看 ${asset.name}`}
                      onOpen={() => setDetailId(asset.id)}
                      thumbnail={<AssetThumb asset={asset} />}
                      selection={
                        <Checkbox
                          aria-label={`选择 ${asset.name}`}
                          checked={selected.includes(asset.id)}
                          onChange={() => toggle(asset.id)}
                        />
                      }
                    >
                      <Text size="xs">
                        {asset.width && asset.height
                          ? `${asset.width}×${asset.height}`
                          : "媒体信息未知"}
                      </Text>
                      <Group justify="space-between" gap={4}>
                        <Text size="xs">{duration(asset.duration)}</Text>
                        <Text size="xs">
                          {asset.usage_count == null
                            ? "未统计"
                            : `使用 ${asset.usage_count} 次`}
                        </Text>
                      </Group>
                    </MediaCard>
                  ))}
                </div>
              ) : (
                <Table.ScrollContainer minWidth={520}>
                  <Table highlightOnHover verticalSpacing="sm">
                    <Table.Thead>
                      <Table.Tr>
                        <Table.Th w={40} />
                        <Table.Th>素材</Table.Th>
                        <Table.Th>时长</Table.Th>
                        <Table.Th>尺寸</Table.Th>
                        <Table.Th>使用</Table.Th>
                      </Table.Tr>
                    </Table.Thead>
                    <Table.Tbody>
                      {assets.map((asset) => (
                        <Table.Tr key={asset.id}>
                          <Table.Td>
                            <Checkbox
                              aria-label={`选择 ${asset.name}`}
                              checked={selected.includes(asset.id)}
                              onChange={() => toggle(asset.id)}
                            />
                          </Table.Td>
                          <Table.Td>
                            <button
                              className="text-button"
                              onClick={() => setDetailId(asset.id)}
                            >
                              {asset.name}
                            </button>
                            {!asset.available && <Status value="offline" />}
                          </Table.Td>
                          <Table.Td>{duration(asset.duration)}</Table.Td>
                          <Table.Td>
                            {asset.width
                              ? `${asset.width}×${asset.height}`
                              : "—"}
                          </Table.Td>
                          <Table.Td>{asset.usage_count ?? "未统计"}</Table.Td>
                        </Table.Tr>
                      ))}
                    </Table.Tbody>
                  </Table>
                </Table.ScrollContainer>
              )}
              {total > 24 && (
                <Group justify="center" mt="lg">
                  <Pagination
                    total={Math.ceil(total / 24)}
                    value={page}
                    onChange={setPage}
                  />
                </Group>
              )}
            </>
          )}
        </Box>
        {detailId && (
          <Modal
            opened={!!detailId}
            onClose={() => setDetailId(null)}
            title={detail?.name || "视频预览"}
            centered
            size={720}
          >
            {detail && (
              <PreviewDetails
                id={detail.id}
                path={detail.path}
                seconds={detail.duration}
                kind={detail.kind}
                available={detail.available}
                asset={detail}
                role={library?.role}
              />
            )}
          </Modal>
        )}
      </div>
      <Modal
        opened={directories}
        onClose={() => setDirectories(false)}
        title="管理素材目录"
        size={720}
        centered
      >
        <Stack gap="md">
          <Text size="sm" c="dimmed">
            每种素材可关联多个目录，自动包含子文件夹。移除关联会保留本地文件。
          </Text>
          <Group gap="sm" wrap="nowrap">
            <Select
              aria-label="管理的素材类型"
              w={170}
              value={libraryId}
              onChange={(value) => {
                setLibraryId(value);
                setDirectoryError("");
                setDirectoryNotice("");
              }}
              allowDeselect={false}
              disabled={busy}
              data={libraries.map((item) => ({
                value: item.id,
                label: roles[item.role],
              }))}
            />
            <Text size="sm" c="dimmed">
              {library?.roots.length || 0} 个目录
            </Text>
            <Button
              ml="auto"
              leftSection={<IconPlus size={16} />}
              onClick={addRoots}
              disabled={busy || !library}
            >
              添加目录
            </Button>
          </Group>
          {busy && (
            <Group gap="xs" role="status" aria-live="polite">
              <Loader size="xs" />
              <Text size="sm">
                {scanned == null
                  ? "正在更新目录…"
                  : `正在读取，已收录 ${scanned} 项素材…`}
              </Text>
            </Group>
          )}
          {directoryError && (
            <Alert color="orange" title="目录读取遇到问题">
              <Text size="xs" className="directory-path">
                {directoryError}
              </Text>
            </Alert>
          )}
          {directoryNotice && (
            <Text size="sm" c="dimmed">
              {directoryNotice}
            </Text>
          )}
          {!library?.roots.length ? (
            <Empty
              title="尚未添加目录"
              description={`为${library ? roles[library.role] : "当前类型"}添加本机目录后，素材会自动显示在这里。`}
            />
          ) : (
            <div className="directory-list">
              {library.roots.map((path) => {
                const status = library.root_status?.find(
                  (item) => item.path === path,
                );
                return (
                  <div className="directory-row" key={path}>
                    <IconFolderOpen
                      size={20}
                      stroke={1.6}
                      className="directory-icon"
                    />
                    <Stack gap={5} className="directory-info">
                      <Group gap="sm">
                        <Text fw={500} size="sm">
                          {path.split(/[\\/]/).filter(Boolean).at(-1) || path}
                        </Text>
                        <Text
                          size="xs"
                          c={
                            !status?.scanned_at
                              ? "dimmed"
                              : status.error || !status.available
                                ? "orange"
                                : "teal"
                          }
                        >
                          {!status?.scanned_at
                            ? "待读取"
                            : status.error
                              ? "读取异常"
                              : status.available
                                ? "可用"
                                : "离线"}
                        </Text>
                        {!!status?.scanned_at && (
                          <Text size="xs" c="dimmed">
                            已收录 {status.active_count || 0} 项
                          </Text>
                        )}
                      </Group>
                      <Text size="xs" className="directory-path">
                        {path}
                      </Text>
                      {status?.error && (
                        <Text
                          size="xs"
                          c="orange"
                          className="directory-path"
                          lineClamp={2}
                          title={status.error}
                        >
                          {status.error}
                        </Text>
                      )}
                    </Stack>
                    <Group gap={6} wrap="nowrap" className="directory-actions">
                      <Button
                        variant="default"
                        size="xs"
                        aria-label={`打开目录 ${path}`}
                        onClick={() => openPath(path).catch(report)}
                      >
                        打开
                      </Button>
                      <Button
                        variant="default"
                        size="xs"
                        aria-label={`刷新目录 ${path}`}
                        onClick={() =>
                          mutate("libraries.scan", {
                            library_id: library.id,
                            roots: [path],
                          })
                        }
                        disabled={busy}
                      >
                        刷新
                      </Button>
                      <Button
                        variant="default"
                        color="red"
                        size="xs"
                        aria-label={`移除目录 ${path}`}
                        onClick={() =>
                          mutate("libraries.remove_root", {
                            library_id: library.id,
                            path,
                          })
                        }
                        disabled={busy}
                      >
                        移除
                      </Button>
                    </Group>
                  </div>
                );
              })}
            </div>
          )}
        </Stack>
      </Modal>
    </>
  );
}
