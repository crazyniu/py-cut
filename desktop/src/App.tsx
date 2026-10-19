import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import {
  Alert,
  AppShell,
  Box,
  Button,
  Divider,
  Group,
  Loader,
  Modal,
  NavLink,
  ScrollArea,
  Select,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import {
  IconAdjustments,
  IconArrowLeft,
  IconCards,
  IconFolderPlus,
  IconLayersSubtract,
  IconMovie,
  IconScissors,
  IconSettings,
  IconTimeline,
} from "@tabler/icons-react";
import {
  connected,
  message,
  onEvent,
  rpc,
  type Agent,
  type Project,
} from "./api";
import { Empty, PageHeading, report } from "./common";
const LibraryPage = lazy(() => import("./LibraryPage"));
const TaskPage = lazy(() => import("./TaskPage"));
const JobsPage = lazy(() => import("./JobsPage"));
const SettingsPage = lazy(() => import("./SettingsPage"));
const pages = [
  { id: "library", label: "素材库", icon: IconCards },
  { id: "montage", label: "混剪", icon: IconTimeline },
  { id: "cover", label: "字幕擦除", icon: IconScissors },
  { id: "dedup", label: "视频去重", icon: IconLayersSubtract },
  { id: "jobs", label: "任务与成片", icon: IconMovie },
  { id: "settings", label: "设置", icon: IconSettings },
];
export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [page, setPage] = useState("library");
  const [create, setCreate] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [codex, setCodex] = useState<"loading" | "ready" | "missing">(
    "loading",
  );
  const checkCodex = useCallback(() => {
    setCodex("loading");
    rpc<Agent[]>("agents.detect")
      .then((items) =>
        setCodex(
          items.some((item) => item.id === "codex" && item.available)
            ? "ready"
            : "missing",
        ),
      )
      .catch(() => setCodex("missing"));
  }, []);
  useEffect(() => {
    if (connected) checkCodex();
  }, [checkCodex]);
  const [connectionError, setConnectionError] = useState("");
  const refresh = useCallback(() => {
    rpc<Project[]>("projects.list")
      .then(setProjects)
      .catch((error) => setError(message(error)));
  }, []);
  useEffect(() => {
    if (connected) refresh();
  }, [refresh]);
  useEffect(() => {
    if (!connected) return;
    let dispose: (() => void) | undefined;
    let closed = false;
    let pending: number | undefined;
    onEvent(() => {
      if (pending !== undefined) return;
      pending = window.setTimeout(() => {
        pending = undefined;
        setRevision((value) => value + 1);
      }, 200);
    })
      .then((unlisten) => {
        if (closed) unlisten();
        else dispose = unlisten;
      })
      .catch((error) => setConnectionError(message(error)));
    return () => {
      closed = true;
      window.clearTimeout(pending);
      dispose?.();
    };
  }, []);
  const project = projects.find((item) => item.id === projectId);
  async function createProject() {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const value = await rpc<Project>("projects.create", {
        name: name.trim(),
      });
      setProjects((items) => [...items, value]);
      setProjectId(value.id);
      setCreate(false);
      setName("");
    } catch (error) {
      report(error);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <AppShell navbar={{ width: 192, breakpoint: 0 }} padding={0}>
        <AppShell.Navbar p="md">
          <Group gap="xs" mb="lg">
            <IconMovie size={25} color="var(--mantine-color-cyan-5)" />
            <Title order={1} size="h3">
              pycut
            </Title>
          </Group>
          {project ? (
            <>
              <Button
                variant="subtle"
                color="gray"
                size="xs"
                leftSection={<IconArrowLeft size={14} />}
                justify="flex-start"
                onClick={() => setProjectId(null)}
                mb="sm"
              >
                所有项目
              </Button>
              <Select
                aria-label="切换项目"
                data={projects.map((item) => ({
                  value: item.id,
                  label: item.name,
                }))}
                value={projectId}
                onChange={(value) => {
                  setProjectId(value);
                  setPage("library");
                }}
                mb="lg"
              />
              {pages.map((item) => (
                <NavLink
                  key={item.id}
                  label={item.label}
                  leftSection={<item.icon size={18} stroke={1.6} />}
                  active={page === item.id}
                  onClick={() => setPage(item.id)}
                  mb={4}
                />
              ))}
            </>
          ) : (
            <NavLink
              active
              label="项目"
              leftSection={<IconCards size={18} />}
            />
          )}
          <Box mt="auto">
            <Divider my="md" />
            <Text size="xs" c="dimmed">
              本地视频工具
            </Text>
            <Text size="xs" c="dimmed" mt={3}>
              素材留在原目录
            </Text>
          </Box>
        </AppShell.Navbar>
        <AppShell.Main>
          {!connected && (
            <Alert color="yellow" radius={0} title="桌面服务未连接">
              当前页面仅供界面预览。请通过 pycut
              桌面应用启动，以读取素材和运行任务。
            </Alert>
          )}
          {connectionError && (
            <Alert color="red" radius={0} title="任务事件连接失败">
              {connectionError}。请重新启动应用恢复实时进度。
            </Alert>
          )}
          <ScrollArea className="main-scroll" type="auto">
            <Box p="xl" className="main-content">
              <Suspense fallback={<Loader size="sm" aria-label="页面加载中" />}>
                {connected && codex !== "ready" ? (
                  codex === "loading" ? (
                    <Group>
                      <Loader size="sm" />
                      <Text>正在检查本机 Codex…</Text>
                    </Group>
                  ) : (
                    <Empty
                      title="请先安装并登录 Codex"
                      description="pycut 使用本机 Codex 提供智能功能。安装并登录后，点击重新检测即可开始。"
                      action={
                        <Button onClick={checkCodex}>重新检测 Codex</Button>
                      }
                    />
                  )
                ) : !project ? (
                  <>
                    <PageHeading
                      title="项目"
                      description="一个产品，一个独立项目。素材、处理任务与成片按项目管理。"
                      actions={
                        <Button
                          leftSection={<IconFolderPlus size={17} />}
                          onClick={() => setCreate(true)}
                          disabled={!connected}
                        >
                          新建项目
                        </Button>
                      }
                    />
                    {error && (
                      <Alert color="red" mb="md">
                        {error}
                        <Button size="xs" variant="subtle" onClick={refresh}>
                          重试
                        </Button>
                      </Alert>
                    )}
                    {projects.length ? (
                      <div className="project-grid">
                        {projects.map((item) => (
                          <button
                            className="project-card"
                            key={item.id}
                            onClick={() => {
                              setProjectId(item.id);
                              setPage("library");
                            }}
                          >
                            <IconAdjustments size={24} stroke={1.4} />
                            <Text fw={600} mt="md">
                              {item.name}
                            </Text>
                            <Text size="xs" c="dimmed" mt="xs" lineClamp={2}>
                              {item.output_dir || "尚未设置输出目录"}
                            </Text>
                            <Text size="sm" c="cyan" mt="lg">
                              进入项目
                            </Text>
                          </button>
                        ))}
                      </div>
                    ) : (
                      <Empty
                        title={
                          connected ? "新建第一个产品项目" : "从桌面应用开始"
                        }
                        description="每个项目都拥有自己的素材库、混剪、处理任务和成片。"
                        action={
                          <Button
                            onClick={() => setCreate(true)}
                            disabled={!connected}
                          >
                            新建项目
                          </Button>
                        }
                      />
                    )}
                  </>
                ) : (
                  <>
                    {page === "library" && (
                      <LibraryPage key={project.id} project={project} />
                    )}
                    {["montage", "cover", "dedup"].includes(page) && (
                      <TaskPage
                        key={`${project.id}-${page}`}
                        project={project}
                        kind={page as "montage" | "cover" | "dedup"}
                        revision={revision}
                        onJobs={() => setPage("jobs")}
                        onSettings={() => setPage("settings")}
                      />
                    )}
                    {page === "jobs" && (
                      <JobsPage
                        key={project.id}
                        project={project}
                        revision={revision}
                      />
                    )}
                    {page === "settings" && (
                      <SettingsPage
                        key={project.id}
                        project={project}
                        onProjectChange={refresh}
                      />
                    )}
                  </>
                )}
              </Suspense>
            </Box>
          </ScrollArea>
        </AppShell.Main>
      </AppShell>
      <Modal
        opened={create}
        onClose={() => setCreate(false)}
        title="新建产品项目"
        centered
      >
        <Stack>
          <TextInput
            label="项目名称"
            placeholder="输入产品名称"
            value={name}
            onChange={(event) => setName(event.currentTarget.value)}
            autoFocus
            onKeyDown={(event) => {
              if (event.key === "Enter") void createProject();
            }}
          />
          <Text size="sm" c="dimmed">
            创建后可添加素材目录，在设置中选择成片保存位置。
          </Text>
          <Button
            onClick={createProject}
            disabled={!name.trim()}
            loading={busy}
          >
            创建项目
          </Button>
        </Stack>
      </Modal>
    </>
  );
}
