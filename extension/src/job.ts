/**
 * The second injected script: reads this page's job posting identity on a button press.
 *
 * Like `collect.ts` it exports nothing, so the built file is a classic script, and it is injected
 * only by the panel under `activeTab` -- never declared in the manifest.
 */

import { readJob } from "./jobinfo";

readJob(document, location.href);
