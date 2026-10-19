import { useEffect } from "react";
import { Badge, Box, Group, Paper, Progress, Text } from "@mantine/core";
import {
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useNodesState,
  type NodeProps,
  type Node,
} from "@xyflow/react";
import { type Job } from "./api";
interface StageData extends Record<string, unknown> {
  title: string;
  subtitle: string;
  state?: string;
  progress?: number;
}
type StageNode = Node<StageData>;
const definition = [
  {
    id: "reference",
    title: "话术与原声",
    subtitle: "选择话术视频及关联字幕",
    x: 0,
    y: 30,
  },
  {
    id: "background",
    title: "背景切片",
    subtitle: "扫描、随机选片与混剪",
    x: 0,
    y: 200,
  },
  { id: "pip", title: "画中画", subtitle: "可选素材库", x: 0, y: 370 },
  {
    id: "compose",
    title: "正文合成",
    subtitle: "画中画、字幕与原声",
    x: 330,
    y: 180,
  },
  { id: "sticker", title: "贴纸", subtitle: "可选本地素材", x: 330, y: 390 },
  { id: "intro", title: "前贴", subtitle: "正文完成后拼接", x: 660, y: 30 },
  {
    id: "output",
    title: "发布成片",
    subtitle: "完整成片写入输出目录",
    x: 660,
    y: 220,
  },
];
function Stage({ data, selected }: NodeProps<StageNode>) {
  return (
    <Paper
      withBorder
      p="md"
      w={218}
      className={`flow-stage ${selected ? "selected" : ""}`}
    >
      <Handle type="target" position={Position.Left} />
      <Group justify="space-between" gap={6}>
        <Text size="sm" fw={600}>
          {data.title}
        </Text>
        {data.state && (
          <Badge
            size="xs"
            color={
              data.state === "运行中"
                ? "cyan"
                : data.state === "失败"
                  ? "red"
                  : "gray"
            }
            variant="light"
          >
            {data.state}
          </Badge>
        )}
      </Group>
      <Text size="xs" c="dimmed" mt={8}>
        {data.subtitle}
      </Text>
      {data.progress != null && (
        <Progress value={data.progress} size={4} mt="sm" />
      )}
      <Handle type="source" position={Position.Right} />
    </Paper>
  );
}
const nodeTypes = { stage: Stage };
const links = [
  ["reference", "compose"],
  ["background", "compose"],
  ["pip", "compose"],
  ["sticker", "compose"],
  ["compose", "output"],
  ["intro", "output"],
];
export default function Workflow(props: {
  job?: Job;
  selected: string;
  onSelect: (id: string) => void;
  enabled: Record<string, boolean>;
  counts: Record<string, number | undefined>;
  reset: number;
}) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}
function Canvas({
  job,
  selected,
  onSelect,
  enabled,
  counts,
  reset,
}: {
  job?: Job;
  selected: string;
  onSelect: (id: string) => void;
  enabled: Record<string, boolean>;
  counts: Record<string, number | undefined>;
  reset: number;
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<StageNode>([]);
  useEffect(() => {
    setNodes(
      definition.map((item) => ({
        id: item.id,
        type: "stage",
        position: { x: item.x, y: item.y },
        data: { title: item.title, subtitle: item.subtitle },
      })),
    );
  }, [reset, setNodes]);
  useEffect(() => {
    const stage = job?.progress?.stage || job?.stage || "";
    let active: string | undefined;
    if (job?.status === "running") {
      if (stage.includes("背景")) active = "background";
      else if (stage.includes("画中画")) active = "compose";
      else if (stage.includes("字幕") || stage.includes("原声"))
        active = "compose";
      else if (stage === "混剪合成")
        active =
          job.progress && job.progress.completed === 0
            ? "reference"
            : job.progress && job.progress.completed === 1
              ? "background"
              : job.progress && job.progress.completed >= 5
                ? "output"
                : "compose";
    }
    setNodes((items) =>
      items.map((node) => {
        const item = definition.find((value) => value.id === node.id)!;
        const disabled = enabled[node.id] === false;
        return {
          ...node,
          selected: node.id === selected,
          data: {
            title: item.title,
            subtitle:
              counts[node.id] != null
                ? `${counts[node.id]} 个${node.id === "reference" ? "已选话术" : "库内素材"}`
                : item.subtitle,
            state: disabled
              ? "已跳过"
              : active === node.id
                ? "运行中"
                : job?.status === "failed" && node.id === "output"
                  ? "失败"
                  : job?.status === "completed" && node.id === "output"
                    ? "已完成"
                    : undefined,
            progress:
              active === node.id && stage !== "混剪合成" && job?.progress?.total
                ? Math.min(
                    100,
                    (job.progress.completed / job.progress.total) * 100,
                  )
                : undefined,
          },
        };
      }),
    );
  }, [job, selected, enabled, counts, setNodes]);
  return (
    <Box className="workflow-canvas">
      <ReactFlow
        nodes={nodes}
        edges={links.map(([source, target]) => ({
          id: `${source}-${target}`,
          source,
          target,
          type: "smoothstep",
          style: {
            stroke:
              enabled[source] === false
                ? "#35404c"
                : nodes.some(
                      (node) =>
                        (node.id === source || node.id === target) &&
                        node.data.state === "运行中",
                    )
                  ? "var(--mantine-color-cyan-6)"
                  : "#557484",
            strokeWidth: 1.5,
          },
        }))}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onNodeClick={(_, node) => onSelect(node.id)}
        nodesConnectable={false}
        edgesReconnectable={false}
        deleteKeyCode={null}
        fitView
        minZoom={0.4}
        maxZoom={1.5}
        colorMode="dark"
        defaultEdgeOptions={{ animated: false }}
      >
        <Controls showInteractive={false} />
      </ReactFlow>
    </Box>
  );
}
