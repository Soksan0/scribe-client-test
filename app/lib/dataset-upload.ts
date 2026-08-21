export const DATASET_ACCEPT = ".csv,.tsv,.xlsx,.sav,.dta,.rds";
export const DATASET_FORMAT_LABEL = "CSV, TSV, XLSX, SAV, DTA, or RDS";
export const MAX_DATASET_BYTES = 250 * 1024 * 1024;

const acceptedExtensions = new Set(DATASET_ACCEPT.split(",").map((value) => value.slice(1)));

export function validateDatasetFile(file: File): string | null {
  const extension = file.name.includes(".") ? file.name.split(".").pop()?.toLowerCase() || "" : "";
  if (!acceptedExtensions.has(extension)) return `“${file.name}” is not supported. Choose a ${DATASET_FORMAT_LABEL} dataset.`;
  if (file.size === 0) return `“${file.name}” is empty. Choose a file that contains data.`;
  if (file.size > MAX_DATASET_BYTES) return `“${file.name}” is larger than Scribe's 250 MB limit.`;
  return null;
}
