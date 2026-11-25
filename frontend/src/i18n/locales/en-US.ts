import type zhCN from "./zh-CN";
import type { DeepStringify } from "./en-US/types";
import enUSCommon from "./en-US/common";
import enUSAdmin from "./en-US/admin";
import enUSDashboard from "./en-US/dashboard";
import enUSCrashes from "./en-US/crashes";
import enUSIssues from "./en-US/issues";
import enUSSymbols from "./en-US/symbols";
import enUSViewers from "./en-US/viewers";

const enUS: DeepStringify<typeof zhCN> = {
  ...enUSCommon,
  ...enUSAdmin,
  ...enUSDashboard,
  ...enUSCrashes,
  ...enUSIssues,
  ...enUSSymbols,
  ...enUSViewers,
};

export default enUS;
