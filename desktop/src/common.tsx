import {
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Paper,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { IconAlertCircle, IconFolder, IconVideo } from "@tabler/icons-react";
import { useEffect, useState, type ReactNode } from "react";
import { notifications } from "@mantine/notifications";
import {
  fileUrl,
  message,
  rpc,
  reveal,
  statusColor,
  statusNames,
  type Asset,
} from "./api";
export const report = (error: unknown) =>
  notifications.show({
    title: "操作未完成",
    message: message(error),
    color: "red",
    autoClose: false,
  });
export function Empty({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <Center className="empty">
      <Stack align="center" gap="sm">
        <IconFolder
          size={32}
          stroke={1.3}
          color="var(--mantine-color-dimmed)"
        />
        <Text fw={600}>{title}</Text>
        {description && (
          <Text c="dimmed" size="sm" ta="center" maw={440}>
            {description}
          </Text>
        )}
        {action}
      </Stack>
    </Center>
  );
}
export function ErrorNotice({
  error,
  retry,
}: {
  error: string;
  retry?: () => void;
}) {
  return (
    <Alert
      color="red"
      icon={<IconAlertCircle size={18} />}
      title="无法读取数据"
    >
      {error}
      {retry && (
        <Button variant="subtle" color="red" size="xs" onClick={retry}>
          重试
        </Button>
      )}
    </Alert>
  );
}
export function PageHeading({
  title,
  description,
  actions,
  status,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  status?: string;
}) {
  return (
    <Group
      gap="md"
      mb="lg"
      justify="space-between"
      align="center"
      className="page-heading"
    >
      <Box miw={0}>
        <Group gap="sm">
          <Title order={2} size="h3">
            {title}
          </Title>
          {status && <Status value={status} />}
        </Group>
        {description && (
          <Text size="sm" c="dimmed" mt={4}>
            {description}
          </Text>
        )}
      </Box>
      {actions && (
        <Group gap="xs" align="center">
          {actions}
        </Group>
      )}
    </Group>
  );
}
export function Status({ value }: { value: string }) {
  return (
    <Badge
      color={statusColor(value)}
      variant="filled"
      size="sm"
      radius={4}
      className="status-tag"
      styles={{
        root: {
          background:
            (
              {
                running: "#6bdcff",
                completed: "#8aebc5",
                queued: "#cab9ff",
                review: "#ffd586",
                partial: "#ffd586",
                failed: "#ffacaa",
                cancelled: "#bdcadc",
                interrupted: "#ffd586",
                cancelling: "#ffd586",
                skipped: "#bdcadc",
                unprocessed: "#bdcadc",
              } as Record<string, string>
            )[value] || "#bdcadc",
          color: "#132233",
        },
      }}
    >
      {statusNames[value] || value}
    </Badge>
  );
}
export function MediaCard({
  thumbnail,
  title,
  label,
  children,
  status,
  overlay,
  selection,
  active,
  disabled,
  onOpen,
}: {
  thumbnail: ReactNode;
  title: string;
  label: string;
  children?: ReactNode;
  status?: string;
  overlay?: ReactNode;
  selection?: ReactNode;
  active?: boolean;
  disabled?: boolean;
  onOpen: () => void;
}) {
  return (
    <Paper withBorder className={`asset-card ${active ? "active" : ""}`}>
      <button
        className="asset-open"
        onClick={onOpen}
        disabled={disabled}
        aria-label={label}
      >
        <Box className="media-card-image">
          {thumbnail}
          {status && (
            <Box className="thumbnail-status">
              <Status value={status} />
            </Box>
          )}
          {overlay}
          <Box className="media-card-caption">
            <Text size="sm" fw={600} lineClamp={2} title={title}>
              {title}
            </Text>
            {children}
          </Box>
        </Box>
      </button>
      {selection && <Box className="thumb-select">{selection}</Box>}
    </Paper>
  );
}
export function AssetThumb({ asset }: { asset: Asset }) {
  const [src, setSrc] = useState("");
  useEffect(() => {
    let disposed = false;
    if (asset.available && asset.kind !== "subtitle")
      rpc<{ path: string }>("media.thumbnail", { asset_id: asset.id })
        .then((result) => {
          if (!disposed) setSrc(fileUrl(result.path));
        })
        .catch(() => {});
    return () => {
      disposed = true;
    };
  }, [asset.id, asset.available, asset.kind]);
  return (
    <Box className="asset-thumb">
      {src ? (
        <img src={src} alt="" loading="lazy" />
      ) : (
        <IconVideo size={26} color="var(--mantine-color-dimmed)" />
      )}
      {!asset.available && (
        <Box className="thumb-badge">
          <Status value="offline" />
        </Box>
      )}
    </Box>
  );
}
export function MediaPreview({
  method = "media.preview",
  id,
  output = false,
  image = false,
  onDuration,
}: {
  method?: string;
  id: string;
  output?: boolean;
  image?: boolean;
  onDuration?: (seconds: number) => void;
}) {
  const [state, setState] = useState<{ path?: string; error?: string }>({});
  useEffect(() => {
    let disposed = false;
    setState({});
    rpc<{ path: string }>(method, output ? { output_id: id } : { asset_id: id })
      .then((result) => {
        if (!disposed) setState({ path: fileUrl(result.path) });
      })
      .catch((error) => {
        if (!disposed) setState({ error: message(error) });
      });
    return () => {
      disposed = true;
    };
  }, [id, method, output]);
  return (
    <Box className="video-preview">
      {state.error ? (
        <Text c="dimmed" size="sm" p="md">
          预览不可用：{state.error}
        </Text>
      ) : state.path ? (
        image ? (
          <img src={state.path} alt="素材预览" className="preview-image" />
        ) : (
          <video
            src={state.path}
            controls
            preload="metadata"
            onLoadedMetadata={(event) =>
              onDuration?.(event.currentTarget.duration)
            }
            onError={() =>
              setState({
                error: "当前文件无法播放，请在系统中打开或重新扫描素材。",
              })
            }
          />
        )
      ) : (
        <Loader size="sm" />
      )}
    </Box>
  );
}

type MediaDetails = {
  duration?: number;
  width?: number;
  height?: number;
  video_codec?: string;
  fps?: number | null;
  size?: number;
  audio?: { codec?: string; sample_rate?: string; channels?: number }[];
};
const strategies: Record<string, string> = {
  reference: "使用完整话术时长、原声与字幕节奏作为混剪基准。",
  background: "每支成片独立随机选择背景片段，使用次数只作统计。",
  pip: "独立随机选片，保留画中画自身字幕。",
  intro: "随机选择一个完整前贴视频，放在正文之前，保留其字幕。",
  sticker: "从所选贴纸库中随机叠加贴纸。",
};
export function PreviewDetails({
  id,
  path,
  seconds,
  output = false,
  kind = "video",
  available = true,
  asset,
  role,
}: {
  id: string;
  path: string;
  seconds?: number | null;
  output?: boolean;
  kind?: string;
  available?: boolean;
  asset?: Asset;
  role?: string;
}) {
  const [length, setLength] = useState(seconds);
  const [info, setInfo] = useState<MediaDetails>({});
  useEffect(() => {
    setLength(seconds);
    setInfo({});
    let disposed = false;
    if (available && kind !== "subtitle")
      rpc<MediaDetails>(
        "media.details",
        output ? { output_id: id } : { asset_id: id },
      )
        .then((value) => {
          if (!disposed) {
            setInfo(value);
            if (value.duration) setLength(value.duration);
          }
        })
        .catch(() => {});
    return () => {
      disposed = true;
    };
  }, [id, seconds, output, available, kind]);
  const rows = [
    ["时长", length != null ? `${length.toFixed(1)} 秒` : "未知"],
    [
      "画面",
      info.width || asset?.width
        ? `${info.width || asset?.width}×${info.height || asset?.height}`
        : "未知",
    ],
    ["视频编码", info.video_codec?.toUpperCase() || "未知"],
    ["帧率", info.fps ? `${Number(info.fps.toFixed(2))} fps` : "未知"],
    [
      "文件大小",
      info.size != null ? `${(info.size / 1024 / 1024).toFixed(1)} MB` : "未知",
    ],
    [
      "音轨",
      info.audio
        ? info.audio.length
          ? info.audio
              .map((audio) =>
                [
                  audio.codec?.toUpperCase(),
                  audio.sample_rate
                    ? `${Number(audio.sample_rate) / 1000} kHz`
                    : "",
                  audio.channels ? `${audio.channels} 声道` : "",
                ]
                  .filter(Boolean)
                  .join(" · "),
              )
              .join("；")
          : "无音轨"
        : "未知",
    ],
    ...(asset
      ? [
          [
            "使用次数",
            asset.usage_count == null ? "未知" : `${asset.usage_count} 次`,
          ],
          ["关联字幕", asset.srt_path?.split(/[\\/]/).pop() || "无"],
        ]
      : []),
  ];
  return (
    <div className="preview-layout">
      {kind !== "subtitle" && available && (
        <MediaPreview
          id={id}
          method={output ? "media.output_preview" : "media.preview"}
          output={output}
          image={kind === "image"}
          onDuration={setLength}
        />
      )}
      <Stack gap="md">
        <dl className="media-details">
          {rows.map(([label, value]) => (
            <div key={label} style={{ display: "contents" }}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        {role && strategies[role] && (
          <Box>
            <Text size="xs" c="dimmed" mb={4}>
              使用策略
            </Text>
            <Text size="sm">{strategies[role]}</Text>
          </Box>
        )}
        <Box>
          <Text size="xs" c="dimmed" mb={4}>
            文件路径
          </Text>
          <Text size="xs" className="preview-path">
            {path}
          </Text>
        </Box>
        <Button variant="default" onClick={() => reveal(path).catch(report)}>
          在文件夹中显示
        </Button>
      </Stack>
    </div>
  );
}

export function VideoThumbnail({
  id,
  output = false,
  available = true,
}: {
  id?: string;
  output?: boolean;
  available?: boolean;
}) {
  const [src, setSrc] = useState("");
  useEffect(() => {
    let disposed = false;
    setSrc("");
    if (id && available)
      rpc<{ path: string }>(
        output ? "media.output_thumbnail" : "media.thumbnail",
        output ? { output_id: id } : { asset_id: id },
      )
        .then((result) => {
          if (!disposed) setSrc(fileUrl(result.path));
        })
        .catch(() => {});
    return () => {
      disposed = true;
    };
  }, [id, output, available]);
  return (
    <Box className="asset-thumb">
      {src ? (
        <img src={src} alt="" loading="lazy" />
      ) : (
        <IconVideo size={32} color="var(--mantine-color-dimmed)" />
      )}
      {!available && (
        <Box className="thumb-badge">
          <Status value="offline" />
        </Box>
      )}
    </Box>
  );
}

export function LoadingPage({ label = "正在加载…" }: { label?: string }) {
  return (
    <Center className="page-loading">
      <Stack align="center" gap="md">
        <Loader size="md" />
        <Text size="sm" c="dimmed">
          {label}
        </Text>
      </Stack>
    </Center>
  );
}
