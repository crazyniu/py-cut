import { useCallback, useEffect, useState } from "react";
import {
  ActionIcon,
  Badge,
  Box,
  Button,
  Checkbox,
  Group,
  Loader,
  Modal,
  Pagination,
  Paper,
  SegmentedControl,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import {
  IconLayoutGrid,
  IconList,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconTrash,
  IconX,
} from "@tabler/icons-react";
import { useDebouncedValue } from "@mantine/hooks";
import {
  AssetThumb,
  Empty,
  ErrorNotice,
  PreviewDetails,
  PageHeading,
  report,
} from "./common";
import {
  chooseDirectories,
  duration,
  message,
  onEvent,
  roles,
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
  const [create, setCreate] = useState(false);
  const [name, setName] = useState("");
  const [role, setRole] = useState<string | null>("background");
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
    try {
      const result = await rpc<{ errors?: { path: string; error: string }[] }>(
        method,
        params,
      );
      if (method === "libraries.add_roots") {
        const scan = await rpc<{ errors?: { path: string; error: string }[] }>(
          "libraries.scan",
          { library_id: params.library_id },
        );
        if (scan.errors?.length)
          report(
            new Error(
              scan.errors
                .map((item) => `${item.path}: ${item.error}`)
                .join("\n"),
            ),
          );
      } else if (result?.errors?.length) {
        report(
          new Error(
            result.errors
              .map((item) => `${item.path}: ${item.error}`)
              .join("\n"),
          ),
        );
      }
      await loadLibraries();
      setRevision((value) => value + 1);
    } catch (error) {
      report(error);
    } finally {
      setBusy(false);
      setScanned(null);
    }
  }
  async function addRoots() {
    if (!library) return;
    try {
      const roots = await chooseDirectories();
      if (roots.length)
        await mutate("libraries.add_roots", { library_id: library.id, roots });
    } catch (error) {
      report(error);
    }
  }
  async function createLibrary() {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const item = await rpc<Library>("libraries.create", {
        project_id: project.id,
        name: name.trim(),
        role,
      });
      await loadLibraries();
      setLibraryId(item.id);
      setCreate(false);
      setName("");
    } catch (error) {
      report(error);
    } finally {
      setBusy(false);
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
        actions={
          <Button
            variant="light"
            leftSection={<IconPlus size={17} />}
            onClick={() => setCreate(true)}
          >
            新建素材库
          </Button>
        }
      />
      <Group mb="md" align="flex-end">
        <Select
          aria-label="素材库"
          placeholder="请选择素材库"
          w={190}
          data={libraries.map((item) => ({
            value: item.id,
            label: `${item.name} · ${item.active_count} 个`,
          }))}
          value={libraryId}
          onChange={setLibraryId}
          allowDeselect={false}
          disabled={busy}
        />
        <Button
          variant="default"
          onClick={() => setDirectories(true)}
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
          w={200}
        />
        <Select
          aria-label="素材类型"
          w={104}
          allowDeselect={false}
          value={kind}
          onChange={(value) => value !== null && setKind(value)}
          data={[
            { value: "", label: "全部类型" },
            { value: "video", label: "视频" },
            { value: "image", label: "图片" },
            { value: "subtitle", label: "字幕" },
          ]}
        />
        <Select
          aria-label="排序"
          w={120}
          data={[
            { value: "name", label: "按名称" },
            { value: "usage", label: "按使用次数" },
            { value: "duration", label: "按时长" },
            { value: "size", label: "按画面尺寸" },
          ]}
          value={sort}
          onChange={setSort}
          allowDeselect={false}
        />
        <SegmentedControl
          aria-label="显示方式"
          value={layout}
          onChange={setLayout}
          data={[
            {
              value: "grid",
              label: <IconLayoutGrid size={16} aria-label="缩略图视图" />,
            },
            {
              value: "list",
              label: <IconList size={16} aria-label="列表视图" />,
            },
          ]}
        />
        <Button
          size="xs"
          variant="default"
          leftSection={<IconRefresh size={14} />}
          onClick={() => library && mutate("libraries.scan", { library_id: library.id })}
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
                  size="xs"
                  value={removed}
                  onChange={setRemoved}
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
                    <Paper
                      withBorder
                      key={asset.id}
                      className={`asset-card ${detailId === asset.id ? "active" : ""}`}
                    >
                      <Box className="thumb-select">
                        <Checkbox
                          aria-label={`选择 ${asset.name}`}
                          checked={selected.includes(asset.id)}
                          onChange={() => toggle(asset.id)}
                        />
                      </Box>
                      <button
                        className="asset-open"
                        onClick={() => setDetailId(asset.id)}
                        aria-label={`查看 ${asset.name}`}
                      >
                        <AssetThumb asset={asset} />
                        <Box p="xs">
                          <Text
                            size="sm"
                            lineClamp={1}
                            fw={500}
                            title={asset.name}
                          >
                            {asset.name}
                          </Text>
                          <Text
                            size="xs"
                            c="dimmed"
                            lineClamp={1}
                            title={asset.path}
                            mt={2}
                          >
                            {asset.path.split(/[\\/]/).slice(-2, -1).join("")}
                          </Text>
                          <Group justify="space-between" gap={4} mt={4}>
                            <Text size="xs" c="dimmed">
                              {duration(asset.duration)}
                            </Text>
                            <Text size="xs" c="dimmed">
                              {asset.usage_count == null
                                ? "未统计"
                                : `使用 ${asset.usage_count} 次`}
                            </Text>
                          </Group>
                        </Box>
                      </button>
                    </Paper>
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
                            {!asset.available && (
                              <Badge ml={6} color="gray" size="xs">
                                离线
                              </Badge>
                            )}
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
              />
            )}
          </Modal>
        )}
      </div>
      <Modal
        opened={directories}
        onClose={() => setDirectories(false)}
        title={`管理目录 · ${library?.name || "素材库"}`}
        centered
      >
        {library && (
          <Stack>
            {" "}
            <Group justify="space-between" mb="sm">
              <Text size="xs" c="dimmed">
                关联目录
              </Text>
              <ActionIcon
                variant="subtle"
                onClick={addRoots}
                aria-label="添加目录"
                disabled={busy}
              >
                <IconPlus size={16} />
              </ActionIcon>
            </Group>
            {library.roots.map((path) => (
              <Group key={path} gap={3} wrap="nowrap" mb="xs">
                <Tooltip
                  label={
                    library.root_status?.find((item) => item.path === path)
                      ?.error || path
                  }
                  multiline
                  w={250}
                >
                  <Text
                    size="xs"
                    c="dimmed"
                    lineClamp={2}
                    style={{ overflowWrap: "anywhere", flex: 1 }}
                  >
                    {path.split(/[\\/]/).filter(Boolean).slice(-2).join(" / ")}
                    {library.root_status?.find((item) => item.path === path)
                      ?.scanned_at &&
                      !library.root_status?.find((item) => item.path === path)
                        ?.available && (
                        <Text component="span" size="xs" c="orange">
                          {" "}
                          · 离线
                        </Text>
                      )}
                    {library.root_status?.find((item) => item.path === path)
                      ?.error && (
                      <Text component="span" size="xs" c="orange">
                        {" "}
                        · 读取异常
                      </Text>
                    )}
                  </Text>
                </Tooltip>
                <ActionIcon
                  size="sm"
                  color="gray"
                  variant="subtle"
                  aria-label={`移除目录 ${path}`}
                  onClick={() =>
                    mutate("libraries.remove_root", {
                      library_id: library.id,
                      path,
                    })
                  }
                  disabled={busy}
                >
                  <IconX size={12} />
                </ActionIcon>
              </Group>
            ))}
            <Button
              fullWidth
              mt="sm"
              variant="default"
              size="xs"
              onClick={addRoots}
              disabled={busy}
            >
              添加目录
            </Button>
          </Stack>
        )}
      </Modal>
      <Modal
        opened={create}
        onClose={() => setCreate(false)}
        title="新建素材库"
        centered
      >
        <Stack>
          <TextInput
            label="素材库名称"
            value={name}
            onChange={(event) => setName(event.currentTarget.value)}
            autoFocus
          />
          <Select
            label="素材用途"
            data={Object.entries(roles).map(([value, label]) => ({
              value,
              label,
            }))}
            value={role}
            onChange={setRole}
            allowDeselect={false}
          />
          <Button
            disabled={!name.trim()}
            loading={busy}
            onClick={createLibrary}
          >
            创建素材库
          </Button>
        </Stack>
      </Modal>
    </>
  );
}
