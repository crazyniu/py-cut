import { invoke, isTauri, convertFileSrc } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
export const connected = isTauri();
export function rpc<T>(
  method: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  if (!connected)
    return Promise.reject(
      new Error("桌面服务未连接。请从 pycut 桌面应用启动。"),
    );
  return invoke<T>("rpc", { method, params });
}
export async function chooseDirectories(): Promise<string[]> {
  const result = await open({
    directory: true,
    multiple: true,
    title: "选择素材目录",
  });
  return result == null ? [] : Array.isArray(result) ? result : [result];
}
export async function chooseDirectory(): Promise<string | null> {
  const result = await open({
    directory: true,
    multiple: false,
    title: "选择输出目录",
  });
  return typeof result === "string" ? result : null;
}
export async function chooseFile(
  title: string,
  extensions: string[] = [],
): Promise<string | null> {
  const result = await open({
    directory: false,
    multiple: false,
    title,
    ...(extensions.length
      ? { filters: [{ name: extensions.join(" / "), extensions }] }
      : {}),
  });
  return typeof result === "string" ? result : null;
}
export const fileUrl = (path: string) => convertFileSrc(path);
export const reveal = (path: string) => invoke("reveal_path", { path });
export const openPath = (path: string) => invoke("open_path", { path });
export interface DesktopEvent {
  event: "job" | "progress" | "log" | "library";
  job_id?: string;
  payload: unknown;
}
export const onEvent = (callback: (event: DesktopEvent) => void) =>
  listen<DesktopEvent>("pycut-event", ({ payload }) => callback(payload));
export interface Project {
  id: string;
  name: string;
  output_dir: string;
  created_at: string;
}
export type Role = "reference" | "background" | "pip" | "intro" | "sticker";
export interface Library {
  id: string;
  project_id: string;
  name: string;
  role: Role;
  roots: string[];
  root_status?: {
    path: string;
    available: boolean | number;
    error: string;
    scanned_at?: string;
    active_count?: number;
  }[];
  active_count: number;
  removed_count: number;
  video_count: number;
  included_video_count: number;
}
export interface Asset {
  id: string;
  path: string;
  name: string;
  kind: "video" | "image" | "subtitle";
  duration: number | null;
  width: number | null;
  height: number | null;
  available: boolean;
  srt_path?: string;
  active_library_ids?: string[];
  usage_count: number | null;
  removed: boolean;
  excluded: boolean;
  error?: string;
}
export interface Use {
  details?: { start?: number; offset?: number; duration?: number };
  output_path?: string;
  path?: string;
  name?: string;
  role?: string;
  created_at?: string;
  [key: string]: unknown;
}
export type JobKind = "montage" | "cover" | "dedup";
export interface TaskItem {
  output_id?: string;
  progress?: { stage?: string; completed?: number; total?: number };
  id: string;
  name: string;
  path: string;
  status: string;
}
export interface Job {
  items?: TaskItem[];
  batch_dir?: string;
  batch_available?: boolean;
  skipped?: number;
  id: string;
  project_id: string;
  kind: JobKind;
  status: string;
  planned: number;
  completed: number;
  failed: number;
  review: number;
  stage?: string;
  progress?: {
    stage: string;
    completed: number;
    total: number;
    detail?: string;
  };
  error?: string;
  created_at: string;
  finished_at?: string;
  logs?: unknown[];
  outputs?: Output[];
}
export interface Output {
  uses?: Use[];
  available?: boolean;
  id: string;
  path: string;
  name: string;
  job_id: string;
  created_at: string;
}
export interface Option {
  name: string;
  label: string;
  type: string;
  default: unknown;
  choices?: unknown[];
  help?: string;
}
export interface Agent {
  id: string;
  name: string;
  path: string;
  installed: boolean;
  authenticated: boolean | null;
  available: boolean;
  supports_vision: boolean;
  detail: string;
}
export const roles: Record<Role, string> = {
  reference: "话术",
  background: "背景切片",
  pip: "画中画",
  intro: "前贴",
  sticker: "贴纸",
};
export const kinds: Record<JobKind, string> = {
  montage: "混剪",
  cover: "字幕擦除",
  dedup: "视频去重",
};
export const statusNames: Record<string, string> = {
  offline: "离线",
  queued: "排队中",
  cancelling: "正在停止",
  running: "处理中",
  completed: "已完成",
  review: "待复查",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
  skipped: "已跳过",
  unprocessed: "未处理",
  deleted: "已删除",
};
export const terminal = (job: Job) =>
  !["queued", "running", "cancelling"].includes(job.status);
export const statusColor = (status: string) =>
  ({
    completed: "teal",
    running: "cyan",
    cancelling: "orange",
    queued: "gray",
    review: "yellow",
    partial: "yellow",
    failed: "red",
    cancelled: "gray",
    interrupted: "orange",
  })[status] || "gray";
export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}
export const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

export const dateTime = (value: string) =>
  new Date(value).toLocaleString("zh-CN", { hour12: false });

export function useSummary(use: Use): string {
  const parts = [
    use.role === "subtitle"
      ? "字幕"
      : roles[use.role as Role] || use.role || "素材",
  ];
  if (typeof use.details?.offset === "number")
    parts.push(`素材起点 ${use.details.offset.toFixed(2)} 秒`);
  if (typeof use.details?.start === "number")
    parts.push(`正文位置 ${use.details.start.toFixed(2)} 秒`);
  if (typeof use.details?.duration === "number")
    parts.push(`取用 ${use.details.duration.toFixed(2)} 秒`);
  return parts.join(" · ");
}
