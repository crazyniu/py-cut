import {
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
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
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <Group justify="space-between" align="flex-start" wrap="wrap" mb="lg">
      <Box>
        <Title order={2} size="h3">
          {title}
        </Title>
        {description && (
          <Text size="sm" c="dimmed" mt={4}>
            {description}
          </Text>
        )}
      </Box>
      {actions}
    </Group>
  );
}
export function Status({ value }: { value: string }) {
  return (
    <Badge color={statusColor(value)} variant="light" size="sm">
      {statusNames[value] || value}
    </Badge>
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
        <Badge color="gray" className="thumb-badge">
          离线
        </Badge>
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

export function PreviewDetails({
  id,
  path,
  seconds,
  output = false,
  kind = "video",
  available = true,
}: {
  id: string;
  path: string;
  seconds?: number | null;
  output?: boolean;
  kind?: string;
  available?: boolean;
}) {
  const [length, setLength] = useState(seconds);
  useEffect(() => {
    setLength(seconds);
  }, [id, seconds]);
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
      <Stack gap="lg" justify="center">
        {kind === "video" && (
          <Box>
            <Text size="sm" c="dimmed">
              视频时长
            </Text>
            <Text mt={4}>
              {length != null ? `${length.toFixed(1)} 秒` : "—"}
            </Text>
          </Box>
        )}
        <Box>
          <Text size="sm" c="dimmed">
            文件路径
          </Text>
          <Text mt={4} size="sm" className="preview-path">
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
        <Badge color="gray" className="thumb-badge">
          离线
        </Badge>
      )}
    </Box>
  );
}
