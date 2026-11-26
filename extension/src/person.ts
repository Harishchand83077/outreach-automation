/**
 * The third injected script: reads this page's person on a button press.
 *
 * Like `job.ts` it exports nothing, so the built file is a classic script, and it is injected
 * only by the panel under `activeTab` -- never declared in the manifest.
 */

import { readPerson } from "./personinfo";

readPerson(document, location.href);
