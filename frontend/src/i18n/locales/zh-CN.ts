import zhCNCommon from "./zh-CN/common";
import zhCNAdmin from "./zh-CN/admin";
import zhCNDashboard from "./zh-CN/dashboard";
import zhCNCrashes from "./zh-CN/crashes";
import zhCNIssues from "./zh-CN/issues";
import zhCNSymbols from "./zh-CN/symbols";
import zhCNViewers from "./zh-CN/viewers";

const zhCN = {
  ...zhCNCommon,
  ...zhCNAdmin,
  ...zhCNDashboard,
  ...zhCNCrashes,
  ...zhCNIssues,
  ...zhCNSymbols,
  ...zhCNViewers,
} as const;

export default zhCN;
