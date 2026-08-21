"use client";
/* eslint-disable @typescript-eslint/no-explicit-any, react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */

import { DragEvent, FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { components } from "./lib/api-schema";
import { DATASET_ACCEPT, validateDatasetFile } from "./lib/dataset-upload";

const API = process.env.NEXT_PUBLIC_SCRIBE_API_URL || "";
const sections = ["overview", "files", "rules", "issues", "exports", "settings"] as const;
const workflowSections = sections.filter((section) => section !== "settings");
type Section = (typeof sections)[number];
type Json = Record<string, any>;
type DecisionPayload = components["schemas"]["DecisionCreate"];

async function api<T = any>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API}${path}`, options);
  } catch {
    throw new Error("Scribe could not reach its local data service. Keep the Scribe starter window open, then click Retry. On Windows, allow Python and Node.js through the firewall if prompted.");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Request failed (${response.status})`);
  }
  return response.json();
}

function parseRoute(pathname: string) {
  const parts = pathname.split("/").filter(Boolean);
  if (parts[0] === "projects" && parts[1]) {
    return { projectId: parts[1], section: (sections.includes(parts[2] as Section) ? parts[2] : "overview") as Section };
  }
  return { projectId: null, section: null as Section | null };
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function ScribeClient() {
  const pathname = usePathname();
  const [route, setRoute] = useState<{ projectId: string | null; section: Section | null }>({ projectId: null, section: null });
  const [routeReady, setRouteReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [projects, setProjects] = useState<Json[]>([]);
  const [trashedProjects, setTrashedProjects] = useState<Json[]>([]);
  const [project, setProject] = useState<Json | null>(null);
  const [files, setFiles] = useState<Json[]>([]);
  const [findings, setFindings] = useState<Json[]>([]);
  const [findingTotal, setFindingTotal] = useState(0);
  const [findingPage, setFindingPage] = useState(0);
  const [findingStatus, setFindingStatus] = useState("all");
  const [rules, setRules] = useState<Json[]>([]);
  const [suggestions, setSuggestions] = useState<Json[]>([]);
  const [relationships, setRelationships] = useState<Json[]>([]);
  const [studyConfig, setStudyConfig] = useState<Json | null>(null);
  const [exports, setExports] = useState<Json[]>([]);
  const [rStatus, setRStatus] = useState<Json | null>(null);
  const [scans, setScans] = useState<Json[]>([]);
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [previewVersion, setPreviewVersion] = useState<"original" | "reviewed">("reviewed");
  const [filePreview, setFilePreview] = useState<Json | null>(null);
  const [activeFinding, setActiveFinding] = useState<string | null>(null);
  const [findingPreview, setFindingPreview] = useState<Json | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(true);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [showReadinessHelp, setShowReadinessHelp] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setRoute(parseRoute(pathname));
    setRouteReady(true);
  }, [pathname]);

  const load = useCallback(async () => {
    if (!routeReady) return;
    setLoading(true);
    setError("");
    try {
      await api("/api/health");
      if (!route.projectId) {
        const [activeProjects, trashProjects] = await Promise.all([api("/api/projects"), api("/api/projects?status=trash")]);
        setProjects(activeProjects); setTrashedProjects(trashProjects);
      } else {
        const [projectData, fileData, scanData] = await Promise.all([
          api(`/api/projects/${route.projectId}`),
          api(`/api/projects/${route.projectId}/files`),
          api(`/api/projects/${route.projectId}/scans/current`),
        ]);
        setProject(projectData);
        setFiles(fileData);
        setScans(scanData);
        setActiveFile((current) => current && fileData.some((item: Json) => item.id === current) ? current : fileData[0]?.id || null);
        if (route.section === "issues") {
          const statusQuery = findingStatus === "all" ? "" : `&status=${encodeURIComponent(findingStatus)}`;
          const result = await api(`/api/projects/${route.projectId}/findings?limit=30&offset=${findingPage * 30}${statusQuery}`);
          setFindings(result.items);
          setFindingTotal(result.total);
          setActiveFinding((current) => current && result.items.some((item: Json) => item.id === current) ? current : result.items[0]?.id || null);
        }
        if (route.section === "rules") {
          const [ruleData, suggestionData, relationshipData, studyData] = await Promise.all([api(`/api/projects/${route.projectId}/rules`), api(`/api/projects/${route.projectId}/rules/suggestions`), api(`/api/projects/${route.projectId}/relationships`), api(`/api/projects/${route.projectId}/study-config`)]);
          setRules(ruleData); setSuggestions(suggestionData); setRelationships(relationshipData); setStudyConfig(studyData);
        }
        if (route.section === "exports") {
          const [exportData, runtimeData] = await Promise.all([
            api(`/api/projects/${route.projectId}/exports`),
            api("/api/system/r-status"),
          ]);
          setExports(exportData);
          setRStatus(runtimeData);
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Scribe could not load this page");
    } finally { setLoading(false); }
  }, [route.projectId, route.section, routeReady, findingPage, findingStatus]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!route.projectId || !activeFile || route.section !== "files") return;
    api(`/api/projects/${route.projectId}/files/${activeFile}/preview?version=${previewVersion}&limit=20`).then(setFilePreview).catch((reason) => setError(reason.message));
  }, [activeFile, previewVersion, route.projectId, route.section]);

  useEffect(() => {
    if (!route.projectId || !activeFinding || route.section !== "issues") return;
    api(`/api/projects/${route.projectId}/findings/${activeFinding}/preview`).then(setFindingPreview).catch((reason) => setError(reason.message));
  }, [activeFinding, route.projectId, route.section]);

  if (!routeReady || loading) return <Centered title="Opening Scribe…" body="Loading local project data." />;
  if (error && !project && route.projectId) return <ConnectionError message={error} retry={load} />;
  if (error && !route.projectId && projects.length === 0) return <ConnectionError message={error} retry={load} />;
  if (!route.projectId) return <ProjectHome projects={projects} trashedProjects={trashedProjects} refresh={load} />;
  if (!project) return <ConnectionError message="Project not found" retry={load} />;

  const pending = project.pending_count;
  const active = findings.find((item) => item.id === activeFinding) || null;

  async function upload(selected?: File) {
    if (!selected) return;
    const validationError = validateDatasetFile(selected);
    if (validationError) { setNotice(""); setError(validationError); return; }
    setUploadBusy(true);
    setNotice(`Profiling ${selected.name}…`); setError("");
    try {
      const result = await api(`/api/projects/${route.projectId}/files?filename=${encodeURIComponent(selected.name)}`, { method: "POST", headers: { "content-type": selected.type || "application/octet-stream" }, body: selected });
      setNotice(`${selected.name}: ${result.row_count.toLocaleString()} rows, ${result.column_count} columns, ${result.finding_count} findings.`);
      await load(); setActiveFile(result.id);
    } catch (reason) { setNotice(""); setError(reason instanceof Error ? reason.message : "Upload failed"); }
    finally { setUploadBusy(false); if (fileInput.current) fileInput.current.value = ""; }
  }

  async function decide(decision: "accepted" | "rejected", editedValue?: string, duplicateSelection?: { retained_row_id: string; removed_row_ids: string[] }) {
    if (!active || reviewBusy) return;
    setReviewBusy(true);
    setNotice("Saving decision and rebuilding the reviewed copy…");
    try {
      const payload: DecisionPayload = { decision, edited_value: editedValue, ...duplicateSelection };
      await api(`/api/projects/${route.projectId}/findings/${active.id}/decision`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
      setNotice(decision === "accepted" ? (editedValue !== undefined ? `Correction saved and accepted for ${active.affected_count} affected row(s). The reviewed copy was rebuilt; the original is unchanged.` : `Accepted correction applied to ${active.affected_count} affected row(s). The reviewed copy was rebuilt; the original is unchanged.`) : "Finding rejected. The original value is retained.");
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Decision failed"); }
    finally { setReviewBusy(false); }
  }

  async function undo() {
    if (!active || reviewBusy) return;
    setReviewBusy(true);
    try { const result = await api(`/api/projects/${route.projectId}/findings/${active.id}/undo`, { method: "POST" }); setNotice(result.cascaded_finding_ids?.length ? `Decision reversed with ${result.cascaded_finding_ids.length} dependent correction(s). All were returned to pending review.` : "Decision reversed and reviewed copy rebuilt."); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Undo failed"); }
    finally { setReviewBusy(false); }
  }

  async function batchDecide(ids: string[], decision: "accepted" | "rejected") {
    if (!ids.length || reviewBusy) return;
    setReviewBusy(true);
    setNotice(`Applying ${ids.length} reviewed decisions…`); setError("");
    try { await api(`/api/projects/${route.projectId}/findings/batch`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ finding_ids: ids, decision }) }); setNotice(`${ids.length} findings ${decision}. Reviewed copies were rebuilt from their originals.`); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Batch review failed"); }
    finally { setReviewBusy(false); }
  }

  async function disposition(dispositionValue: "acknowledged" | "false_positive" | "deferred", rationale: string) {
    if (!active || reviewBusy) return;
    setReviewBusy(true); setError("");
    try {
      await api(`/api/projects/${route.projectId}/findings/${active.id}/disposition`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ disposition: dispositionValue, rationale }) });
      setNotice(dispositionValue === "acknowledged" ? "Limitation acknowledged with your rationale." : dispositionValue === "false_positive" ? "Finding marked as a false positive." : "Finding deferred for later review.");
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Disposition failed"); }
    finally { setReviewBusy(false); }
  }

  async function manualOperation(payload: Json) {
    if (!active || reviewBusy) return;
    setReviewBusy(true); setError(""); setNotice("Validating the evidence and rebuilding the reviewed copy…");
    try {
      await api(`/api/projects/${route.projectId}/manual-operations`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...payload, file_id: active.file_id, source_finding_id: active.id }) });
      setNotice("Evidence-backed operation applied. The original remains unchanged and the decision can be undone.");
      await load();
    } catch (reason) { setNotice(""); setError(reason instanceof Error ? reason.message : "Manual operation failed"); }
    finally { setReviewBusy(false); }
  }

  async function reanalyze() {
    setNotice("Running the current engine against the reviewed data…"); setError("");
    try { await api(`/api/projects/${route.projectId}/reanalyze`, { method: "POST" }); setNotice("Current-engine scan completed. Legacy decisions remain in the audit but do not alter this reviewed copy."); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Validation scan failed"); }
  }

  async function createExport(kind: "review" | "verified") {
    setNotice("Building and validating export files…"); setError("");
    try { await api(`/api/projects/${route.projectId}/exports`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ kind }) }); setNotice(kind === "review" ? "Provisional review package is ready." : "Verified clean export is ready."); setExports(await api(`/api/projects/${route.projectId}/exports`)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Export failed"); }
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <Link className="brand" href="/"><span className="brand-mark">S</span><span>Scribe</span></Link>
        <nav className="main-nav" aria-label="Project navigation">
          {sections.map((item) => <Link key={item} href={`/projects/${route.projectId}/${item}`} className={route.section === item ? "nav-item active" : "nav-item"}><span className="nav-icon">{({ overview: "⌂", files: "□", rules: "✓", issues: "△", exports: "⇧", settings: "⚙" } as Json)[item]}</span><span>{item[0].toUpperCase() + item.slice(1)}</span>{item === "issues" && <span className="nav-count">{pending}</span>}</Link>)}
        </nav>
        <div className="privacy-card"><strong>▣ Local by default</strong><p>Core cleaning stays on-device. Gemini is optional and requires consent per request.</p></div>
      </aside>
      <section className="workspace">
        <header className="project-header">
          <div><h1>{project.name}</h1><p>Dataset QA · {project.file_count} files · {project.pending_count} pending findings</p></div>
          <div className="header-actions"><button className="secondary-button" disabled={uploadBusy} onClick={() => fileInput.current?.click()}>{uploadBusy ? "Uploading…" : "Add dataset"}</button><input ref={fileInput} type="file" accept={DATASET_ACCEPT} hidden onChange={(event) => void upload(event.target.files?.[0])}/><div className="local-badge"><span>◇</span><strong>Local by default</strong><small>Gemini is opt-in</small></div></div>
        </header>
        {(notice || error) && <div className={error ? "message error" : "message"} role="status">{error || notice}<button aria-label="Dismiss message" onClick={() => { setError(""); setNotice(""); }}>×</button></div>}
        {Boolean(project.needs_rescan) && <div className="migration-warning"><div><strong>Current-engine validation required</strong><p>Older corrections were preserved in the audit and quarantined. Run a fresh scan before trusting readiness or exports.</p></div><button onClick={reanalyze}>Run current scan</button></div>}
        <nav className="stepper" aria-label="Project progress">{workflowSections.map((item, index) => <Link key={item} href={`/projects/${route.projectId}/${item}`} className={route.section === item ? "current" : ""}><span>{index + 1}</span><strong>{item === "overview" ? "Overview" : item[0].toUpperCase() + item.slice(1)}</strong></Link>)}</nav>

        {route.section === "overview" && <><Overview project={project} onHelp={() => setShowReadinessHelp(true)} /><ChecklistPanel readiness={project.readiness} /></>}
        {route.section === "files" && <FilesView projectId={route.projectId} files={files} preview={filePreview} activeFile={activeFile} selectFile={setActiveFile} chooseFile={() => fileInput.current?.click()} upload={upload} uploadBusy={uploadBusy} previewVersion={previewVersion} setPreviewVersion={setPreviewVersion} scans={scans} refresh={load} setNotice={setNotice} setError={setError} />}
        {route.section === "rules" && <><RuleAutomation projectId={route.projectId} suggestions={suggestions} refresh={load} setNotice={setNotice} setError={setError} /><StudyConfiguration projectId={route.projectId} files={files} current={studyConfig} refresh={load} setNotice={setNotice} setError={setError} /><RulesView projectId={route.projectId} files={files} rules={rules} suggestions={suggestions} refresh={load} setNotice={setNotice} setError={setError} /><RuleMaintenance projectId={route.projectId} files={files} rules={rules} refresh={load} setNotice={setNotice} setError={setError} /><AdvancedRules projectId={route.projectId} files={files} relationships={relationships} refresh={load} setNotice={setNotice} setError={setError} /><GeminiAssistant projectId={route.projectId} files={files} refresh={load} setNotice={setNotice} setError={setError} /></>}
        {route.section === "issues" && <><BatchReview findings={findings} batchDecide={batchDecide} busy={reviewBusy} /><IssuesView findings={findings} total={findingTotal} page={findingPage} setPage={setFindingPage} status={findingStatus} setStatus={(value: string) => { setFindingStatus(value); setFindingPage(0); }} active={active} preview={findingPreview} select={setActiveFinding} decide={decide} disposition={disposition} manualOperation={manualOperation} undo={undo} detailsOpen={detailsOpen} closeDetails={() => setDetailsOpen(false)} openDetails={() => setDetailsOpen(true)} busy={reviewBusy} /></>}
        {route.section === "exports" && <ExportsView exports={exports} createExport={createExport} readiness={project.readiness} rStatus={rStatus} />}
        {route.section === "settings" && <SettingsView project={project} refresh={load} setNotice={setNotice} setError={setError} />}
      </section>
      {showReadinessHelp && <div className="modal-backdrop" role="presentation" onClick={() => setShowReadinessHelp(false)}><div className="modal" role="dialog" aria-modal="true" aria-labelledby="readiness-title" onClick={(event) => event.stopPropagation()}><button className="modal-close" aria-label="Close readiness explanation" onClick={() => setShowReadinessHelp(false)}>×</button><h2 id="readiness-title">How readiness works</h2><p>The score measures completed applicable checklist sections. File integrity, structure, reproducibility, and final validation count twice. Missing evidence never counts as a pass, and critical failures cap or reset the score.</p></div></div>}
    </main>
  );
}

function ProjectHome({ projects, trashedProjects, refresh }: { projects: Json[]; trashedProjects: Json[]; refresh: () => Promise<void> }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const [view, setView] = useState<"active" | "trash">("active");
  const [confirmations, setConfirmations] = useState<Record<string, string>>({});
  async function create(event: FormEvent) { event.preventDefault(); setError(""); try { const project = await api("/api/projects", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name, description }) }); window.location.href = `/projects/${project.id}/overview`; } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not create project"); } }
  async function restore(projectId: string) { setError(""); try { await api(`/api/projects/${projectId}/restore`, { method: "POST" }); await refresh(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not restore project"); } }
  async function permanentlyDelete(project: Json) { setError(""); try { await api(`/api/projects/${project.id}`, { method: "DELETE", headers: { "content-type": "application/json" }, body: JSON.stringify({ project_name: confirmations[project.id] || "" }) }); setConfirmations((current) => ({ ...current, [project.id]: "" })); await refresh(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not permanently delete project"); } }
  const visibleProjects = view === "active" ? projects : trashedProjects;
  return <main className="project-home"><section className="home-intro"><div className="brand large"><span className="brand-mark">S</span><span>Scribe</span></div><p className="eyebrow">LOCAL RESEARCH DATA QA</p><h1>Clean research data without losing control.</h1><p>Create one project per study. Scribe preserves every original and asks before changing a reviewed copy.</p></section><section className="project-panel"><form onSubmit={create}><h2>Create a project</h2><label>Project name<input value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. Maternal Health Survey" required /></label><label>Description <span>(optional)</span><textarea value={description} onChange={(event) => setDescription(event.target.value)} /></label>{error && <p className="form-error" role="alert">{error}</p>}<button className="primary-button" type="submit">Create project</button></form><div className="project-list"><div className="project-list-heading"><h2>{view === "active" ? "Recent projects" : "Trash"}</h2><div className="version-switch"><button className={view === "active" ? "active" : ""} onClick={() => setView("active")}>Active</button><button className={view === "trash" ? "active" : ""} onClick={() => setView("trash")}>Trash ({trashedProjects.length})</button></div></div>{visibleProjects.length === 0 ? <div className="empty-small">{view === "active" ? "No projects yet. Create your first study above." : "Trash is empty."}</div> : visibleProjects.map((project) => view === "active" ? <a key={project.id} href={`/projects/${project.id}/overview`}><strong>{project.name}</strong><span>{project.file_count} files · {project.pending_count} pending · readiness {project.readiness.score}</span></a> : <div className="trash-project" key={project.id}><div><strong>{project.name}</strong><span>Moved to Trash {new Date(project.deleted_at).toLocaleString()}</span></div><div className="trash-actions"><button onClick={() => restore(project.id)}>Restore</button><label>Type <strong>{project.name}</strong> to permanently delete<input aria-label={`Confirm permanent deletion of ${project.name}`} value={confirmations[project.id] || ""} onChange={(event) => setConfirmations((current) => ({ ...current, [project.id]: event.target.value }))} /></label><button className="danger-button" disabled={confirmations[project.id] !== project.name} onClick={() => permanentlyDelete(project)}>Delete permanently</button></div></div>)}</div></section></main>;
}

function Overview({ project, onHelp }: { project: Json; onHelp: () => void }) {
  const readiness = project.readiness; const counts = readiness.unresolved_by_severity || {};
  return <><section className="readiness-card"><div className="score"><strong>{readiness.score}</strong><span>{readiness.status.replaceAll("_", " ")}</span></div><div><h2>Project readiness</h2><p>{project.pending_count ? `${project.pending_count} findings still need review.` : project.file_count ? "All detected findings have decisions." : "Upload a dataset to begin."}</p><button className="text-button" onClick={onHelp}>How readiness works</button></div>{["high", "medium", "low"].map((severity) => <div className={`severity ${severity}`} key={severity}><strong>{counts[severity] || 0}</strong><span>{severity}</span></div>)}</section><section className="overview-grid"><article><h2>Files</h2><strong>{project.file_count}</strong><p>Immutable originals in this project</p><Link href={`/projects/${project.id}/files`}>Review inventory →</Link></article><article><h2>Rules</h2><strong>{project.rule_count}</strong><p>Confirmed validation rules</p><Link href={`/projects/${project.id}/rules`}>Confirm rules →</Link></article><article><h2>Findings</h2><strong>{project.finding_count}</strong><p>{project.pending_count} awaiting decisions</p><Link href={`/projects/${project.id}/issues`}>Review findings →</Link></article></section></>;
}

function SettingsView({ project, refresh, setNotice, setError }: { project: Json; refresh: () => Promise<void>; setNotice: (value: string) => void; setError: (value: string) => void }) {
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description || "");
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try { await api(`/api/projects/${project.id}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ name, description }) }); setNotice("Project settings saved."); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not save project settings"); }
    finally { setBusy(false); }
  }
  async function trash() {
    setBusy(true); setError("");
    try { await api(`/api/projects/${project.id}/trash`, { method: "POST" }); window.location.href = "/"; }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not move project to Trash"); setBusy(false); }
  }
  return <section className="settings-view"><div className="page-title"><div><p className="eyebrow">PROJECT SETTINGS</p><h2>Settings</h2><p>Edit project details or move this study to recoverable Trash.</p></div></div><form className="rule-form settings-form" onSubmit={save}><label>Project name<input value={name} onChange={(event) => setName(event.target.value)} required /></label><label>Description<textarea value={description} onChange={(event) => setDescription(event.target.value)} /></label><button className="primary-button" disabled={busy || !name.trim()}>Save settings</button></form><div className="danger-zone"><div><h3>Move project to Trash</h3><p>The project becomes read-only and disappears from Active projects. Originals, hashes, decisions, versions, and audit history remain available if you restore it.</p></div><button className="danger-button" disabled={busy} onClick={trash}>Move to Trash</button></div></section>;
}

function ChecklistPanel({ readiness }: { readiness: Json }) {
  const checklist = readiness?.checklist || [];
  if (!checklist.length) return null;
  return <section className="checklist-panel"><div className="checklist-heading"><div><p className="eyebrow">CLEAN DATA CHECKLIST</p><h2>{readiness.clean ? "All applicable checks cleared" : "Checks still require attention"}</h2></div><span className={readiness.clean ? "clean-label" : "review-label"}>{readiness.clean ? "Clean" : "Provisional"}</span></div><div className="checklist-grid">{checklist.map((item: Json) => <div key={item.number} className={item.status}><span>{item.status === "pass" ? "✓" : item.status === "attention" ? "!" : "—"}</span><div><strong>{item.number}. {item.name}</strong><small>{item.status === "attention" ? item.unresolved_categories.join(", ").replaceAll("_", " ") || "Needs confirmation" : item.status.replaceAll("_", " ")}</small></div></div>)}</div></section>;
}

function ParsingConfiguration({ projectId, file, refresh, setNotice, setError }: any) {
  const initial = file.profile?.parsing_config || {};
  const [open, setOpen] = useState(file.parsing?.status !== "confirmed");
  const [headerRow, setHeaderRow] = useState(String(initial.header_row || 1));
  const [delimiter, setDelimiter] = useState(initial.delimiter || file.delimiter || ",");
  const [encoding, setEncoding] = useState(initial.encoding || file.encoding || "utf-8");
  const [locale, setLocale] = useState(initial.date_locale || "");
  const [missing, setMissing] = useState((initial.missing_tokens || []).join(", "));
  const [identifiers, setIdentifiers] = useState((initial.identifier_columns || file.profile?.candidate_id_columns || []).join(", "));
  const [busy, setBusy] = useState(false);
  useEffect(() => { const config = file.profile?.parsing_config || {}; setHeaderRow(String(config.header_row || 1)); setDelimiter(config.delimiter || file.delimiter || ","); setEncoding(config.encoding || file.encoding || "utf-8"); setLocale(config.date_locale || ""); setMissing((config.missing_tokens || []).join(", ")); setIdentifiers((config.identifier_columns || file.profile?.candidate_id_columns || []).join(", ")); setOpen(file.parsing?.status !== "confirmed"); }, [file.id]);
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await api(`/api/projects/${projectId}/files/${file.id}/parsing-config`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ header_row: Number(headerRow), delimiter, encoding, date_locale: locale || null, missing_tokens: missing.split(",").map((value) => value.trim()), identifier_columns: identifiers.split(",").map((value) => value.trim()).filter(Boolean), variable_labels: {} }) });
      setNotice("Parsing assumptions confirmed. Scribe rebuilt the canonical snapshot and rescanned the original."); setOpen(false); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Parsing configuration failed"); }
    finally { setBusy(false); }
  }
  return <section className="parsing-card"><div className="parsing-heading"><div><p className="eyebrow">PARSING ASSUMPTIONS</p><h3>{file.parsing?.status === "confirmed" ? "Confirmed" : "Confirm before relying on the scan"}</h3><p>Tell Scribe where the real table begins and which values must remain identifiers or missing codes.</p></div><button onClick={() => setOpen((current) => !current)}>{open ? "Close" : "Review parsing"}</button></div>{open && <form onSubmit={save}><label>Header row<input type="number" min="1" max="100" value={headerRow} onChange={(event) => setHeaderRow(event.target.value)} /></label><label>Delimiter<select value={delimiter} onChange={(event) => setDelimiter(event.target.value)}><option value=",">Comma</option><option value="\t">Tab</option><option value=";">Semicolon</option><option value="|">Pipe</option></select></label><label>Encoding<input value={encoding} onChange={(event) => setEncoding(event.target.value)} /></label><label>Date convention<select value={locale} onChange={(event) => setLocale(event.target.value)}><option value="">Not confirmed</option><option value="day_first">Day first</option><option value="month_first">Month first</option><option value="year_first">Year first</option></select></label><label>Missing tokens<input value={missing} onChange={(event) => setMissing(event.target.value)} placeholder="N/A, -9, refused" /></label><label>Identifier columns<input value={identifiers} onChange={(event) => setIdentifiers(event.target.value)} placeholder="participant_id" /></label><button className="primary-button" disabled={busy}>{busy ? "Rebuilding…" : "Confirm and rescan"}</button></form>}</section>;
}

function FilesView({ projectId, files, preview, activeFile, selectFile, chooseFile, upload, uploadBusy, previewVersion, setPreviewVersion, scans, refresh, setNotice, setError }: any) {
  const [dragging, setDragging] = useState(false);
  const active = files.find((file: Json) => file.id === activeFile);
  const scan = scans.find((item: Json) => item.file_id === activeFile);
  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (!uploadBusy) void upload(event.dataTransfer.files?.[0]);
  }
  return <section><div className="page-title"><div><p className="eyebrow">FILE INVENTORY</p><h2>Research datasets</h2><p>Original files remain unchanged. Compare the uploaded source with the current reviewed version.</p></div><button className="primary-button" disabled={uploadBusy} onClick={chooseFile}>{uploadBusy ? "Uploading…" : "Upload dataset"}</button></div><div className={`upload-dropzone ${dragging ? "dragging" : ""} ${uploadBusy ? "busy" : ""}`} onDragEnter={(event) => { event.preventDefault(); setDragging(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }} onDrop={handleDrop}><strong>{uploadBusy ? "Reading and checking your dataset…" : "Drop a dataset here"}</strong><span>or <button type="button" disabled={uploadBusy} onClick={chooseFile}>choose a file</button> · CSV, TSV, XLSX, SAV, DTA, or RDS · up to 250 MB</span></div>{files.length === 0 ? <Empty title="No datasets uploaded" body="Drop a supported dataset above or choose a file to begin profiling."/> : <><div className="two-column"><div className="file-list">{files.map((file: Json) => <button key={file.id} onClick={() => selectFile(file.id)} className={activeFile === file.id ? "selected" : ""}><strong>{file.filename}</strong><span>{file.format.toUpperCase()} · {formatBytes(file.size_bytes)} · {file.row_count.toLocaleString()} rows × {file.column_count} columns</span><small>{file.finding_count} findings · {file.reviewed_version ? `reviewed v${file.reviewed_version.version_number}` : "original only"}</small></button>)}</div><div><div className="version-switch" role="group" aria-label="Dataset version"><button className={previewVersion === "original" ? "active" : ""} onClick={() => setPreviewVersion("original")}>Original</button><button className={previewVersion === "reviewed" ? "active" : ""} onClick={() => setPreviewVersion("reviewed")} disabled={!active?.reviewed_version} title={!active?.reviewed_version ? "No accepted transformations yet" : "Show the rebuilt reviewed copy"}>Reviewed</button></div><DataTable preview={preview}/></div></div>{active && <><ParsingConfiguration projectId={projectId} file={active} refresh={refresh} setNotice={setNotice} setError={setError} /><div className="integrity-grid"><article><span>Original SHA-256</span><code title={active.sha256}>{active.sha256}</code><strong>Unchanged since upload</strong></article><article><span>Current scan</span><strong>{scan ? `${scan.engine_version} · ${scan.status}` : "No completed scan"}</strong><small>{scan?.finding_count ?? 0} evidence-backed findings</small></article><article><span>Schema fingerprint</span><code title={active.profile?.schema_fingerprint}>{active.profile?.schema_fingerprint || "Not recorded"}</code><small>{active.profile?.encoding || active.encoding} · delimiter {JSON.stringify(active.profile?.delimiter || active.delimiter)}</small></article></div></>}</>}</section>;
}

function StudyConfiguration({ projectId, files, current, refresh, setNotice, setError }: any) {
  const [open, setOpen] = useState(false);
  const [fileId, setFileId] = useState(files[0]?.id || "");
  const file = files.find((item: Json) => item.id === fileId) || files[0];
  const columns = (file?.profile?.columns || []).map((item: Json) => item.name);
  const config = current?.config || {};
  const [participantKeys, setParticipantKeys] = useState((config.participant_keys || []).join(", "));
  const [itemColumns, setItemColumns] = useState((config.item_groups?.[0]?.columns || []).join(", "));
  const [scaleMin, setScaleMin] = useState(config.item_groups?.[0]?.minimum ?? "");
  const [scaleMax, setScaleMax] = useState(config.item_groups?.[0]?.maximum ?? "");
  const [requiredColumns, setRequiredColumns] = useState((config.completion?.required_columns || []).join(", "));
  const [completionPercent, setCompletionPercent] = useState(config.completion?.minimum_answered_percent ?? "");
  const [duration, setDuration] = useState(config.timestamp_columns?.duration || "");
  const [attentionColumn, setAttentionColumn] = useState(config.attention_checks?.[0]?.column || "");
  const [attentionExpected, setAttentionExpected] = useState((config.attention_checks?.[0]?.expected_values || []).join(", "));
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    const split = (value: string) => value.split(",").map((item) => item.trim()).filter(Boolean);
    const items = split(itemColumns); const required = split(requiredColumns); const attentionValues = split(attentionExpected);
    const payload = { participant_keys: split(participantKeys), allowed_repeats: false, item_groups: items.length ? [{ name: "Configured questionnaire items", columns: items, minimum_answered: 3, minimum: scaleMin === "" ? null : Number(scaleMin), maximum: scaleMax === "" ? null : Number(scaleMax) }] : [], timestamp_columns: duration ? { duration, speeder_factor: 0.33 } : {}, completion: required.length && completionPercent !== "" ? { required_columns: required, minimum_answered_percent: Number(completionPercent) } : {}, attention_checks: attentionColumn && attentionValues.length ? [{ column: attentionColumn, expected_values: attentionValues }] : [], skip_rules: [], missing_codes: {}, cross_field_rules: [] };
    try { await api(`/api/projects/${projectId}/study-config`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) }); setNotice("Study configuration saved. Run the full checklist scan to create configuration-linked quality flags."); setOpen(false); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Study configuration failed"); }
    finally { setBusy(false); }
  }
  if (!files.length) return null;
  return <section className="study-config"><div className="study-heading"><div><p className="eyebrow">QUESTIONNAIRE CONTEXT</p><h2>Survey quality configuration</h2><p>Scribe only checks speed, straightlining, completion, attention, and participant identity after you confirm the relevant study fields.</p></div><button onClick={() => setOpen((value) => !value)}>{open ? "Close setup" : current?.status === "confirmed" ? "Edit setup" : "Configure study"}</button></div>{current?.status === "confirmed" && !open && <div className="configured-summary">Configuration v{current.version} is active. Rescan after changes so every flag records this configuration hash.</div>}{open && <form onSubmit={save}><label>Dataset<select value={fileId} onChange={(event) => setFileId(event.target.value)}>{files.map((item: Json) => <option key={item.id} value={item.id}>{item.filename}</option>)}</select></label><label>Participant key columns<input value={participantKeys} onChange={(event) => setParticipantKeys(event.target.value)} placeholder="participant_id" /></label><label>Likert / matrix item columns<input value={itemColumns} onChange={(event) => setItemColumns(event.target.value)} placeholder={columns.slice(0, 4).join(", ")} /></label><div className="inline-fields"><label>Scale minimum<input type="number" value={scaleMin} onChange={(event) => setScaleMin(event.target.value)} /></label><label>Scale maximum<input type="number" value={scaleMax} onChange={(event) => setScaleMax(event.target.value)} /></label></div><label>Required completion columns<input value={requiredColumns} onChange={(event) => setRequiredColumns(event.target.value)} /></label><label>Minimum answered %<input type="number" min="0" max="100" value={completionPercent} onChange={(event) => setCompletionPercent(event.target.value)} /></label><label>Duration column (seconds)<select value={duration} onChange={(event) => setDuration(event.target.value)}><option value="">Not available</option>{columns.map((column: string) => <option key={column}>{column}</option>)}</select></label><label>Attention-check column<select value={attentionColumn} onChange={(event) => setAttentionColumn(event.target.value)}><option value="">Not configured</option>{columns.map((column: string) => <option key={column}>{column}</option>)}</select></label><label>Expected attention value(s)<input value={attentionExpected} onChange={(event) => setAttentionExpected(event.target.value)} /></label><button className="primary-button" disabled={busy}>{busy ? "Saving…" : "Save study configuration"}</button></form>}</section>;
}

function RuleAutomation({ projectId, suggestions, refresh, setNotice, setError }: any) {
  const recommended = suggestions.filter((item: Json) => item.recommended);
  async function confirmRecommended() {
    try { const result = await api(`/api/projects/${projectId}/rules/auto-confirm`, { method: "POST" }); setNotice(`${result.confirmed_count} recommended rules confirmed automatically; ${result.finding_count} violations found for review.`); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Recommended rules could not be confirmed"); }
  }
  async function reanalyze() {
    try { const result = await api(`/api/projects/${projectId}/reanalyze`, { method: "POST" }); setNotice(`Full checklist scan completed for ${result.file_count} file(s); ${result.finding_count} issues found for review.`); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Full checklist scan failed"); }
  }
  return <section className="automation-card"><div><p className="eyebrow">AUTOMATED SETUP</p><h2>Start with evidence-based rules</h2><p>Scribe inferred {suggestions.length} possible rules from names, values, and types. {recommended.length} are strong enough to confirm together; study-specific ranges and codes still need your judgment.</p></div><div className="automation-actions"><button onClick={reanalyze}>Run full checklist scan</button><button className="primary-button" disabled={!recommended.length} title={recommended.length ? "Confirm recommended identifier and type rules" : "No new recommended rules"} onClick={confirmRecommended}>Confirm {recommended.length} recommended rules</button></div></section>;
}

function RulesView({ projectId, files, rules, suggestions, refresh, setNotice, setError }: any) {
  const [fileId, setFileId] = useState(files[0]?.id || ""); const file = files.find((item: Json) => item.id === fileId); const columns = file?.profile?.columns || [];
  const [type, setType] = useState("unique"); const [column, setColumn] = useState(columns[0]?.name || ""); const [minimum, setMinimum] = useState(""); const [maximum, setMaximum] = useState(""); const [values, setValues] = useState("");
  useEffect(() => { setColumn((files.find((item: Json) => item.id === fileId)?.profile?.columns || [])[0]?.name || ""); }, [fileId, files]);
  async function submit(event: FormEvent) { event.preventDefault(); const parameters: Json = { column }; if (["range", "scale"].includes(type)) { parameters.minimum = minimum === "" ? null : Number(minimum); parameters.maximum = maximum === "" ? null : Number(maximum); } if (type === "allowed_values") parameters.values = values.split(",").map((value) => value.trim()).filter(Boolean); if (type === "missing_codes") parameters.codes = values.split(",").map((value) => value.trim()).filter(Boolean); if (type === "type") parameters.expected = values || "number"; if (type === "pattern") parameters.pattern_type = values || "email"; try { const result = await api(`/api/projects/${projectId}/rules`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ file_id: fileId, name: `${column} ${type.replaceAll("_", " ")}`, rule_type: type, parameters }) }); setNotice(result.duplicate ? `“${result.name}” already exists and is active. No duplicate rule was added.` : `Rule confirmed. ${result.finding_count} violations detected.`); await refresh(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Rule could not be confirmed"); } }
  async function confirmSuggestion(suggestion: Json) { try { const result = await api(`/api/projects/${projectId}/rules`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(suggestion) }); setNotice(result.duplicate ? `“${result.name}” is already active. No duplicate rule was added.` : `Suggested rule confirmed. ${result.finding_count} violations detected.`); await refresh(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Rule could not be confirmed"); } }
  async function disable(id: string) { await api(`/api/projects/${projectId}/rules/${id}`, { method: "DELETE" }); setNotice("Rule disabled."); await refresh(); }
  if (!files.length) return <Empty title="Upload data before adding rules" body="Scribe needs columns to propose and validate rules."/>;
  const confirmedRules = rules.filter((rule: Json) => rule.status === "confirmed");
  const disabledCount = rules.length - confirmedRules.length;
  return <section><div className="page-title"><div><p className="eyebrow">ADVANCED CHECKS & ASSUMPTIONS</p><h2>Study-specific rules</h2><p>Basic quality checks run automatically. Add only assumptions that require study knowledge; conflicting active rules are blocked.</p></div></div><div className="rules-grid"><form className="rule-form" onSubmit={submit}><h3>Add an assumption</h3><label>Dataset<select value={fileId} onChange={(event) => setFileId(event.target.value)}>{files.map((item: Json) => <option key={item.id} value={item.id}>{item.filename}</option>)}</select></label><label>Check type<select value={type} onChange={(event) => setType(event.target.value)}>{["unique","required","missing_codes","allowed_values","range","scale","pattern","type","date"].map((item) => <option key={item} value={item}>{item.replaceAll("_", " ")}</option>)}</select></label><label>Column<select value={column} onChange={(event) => setColumn(event.target.value)}>{columns.map((item: Json) => <option key={item.name}>{item.name}</option>)}</select></label>{["range", "scale"].includes(type) && <div className="inline-fields"><label>Minimum<input type="number" value={minimum} onChange={(event) => setMinimum(event.target.value)}/></label><label>Maximum<input type="number" value={maximum} onChange={(event) => setMaximum(event.target.value)}/></label></div>}{["allowed_values","missing_codes","type","pattern"].includes(type) && <label>{type === "type" ? "Expected type" : type === "pattern" ? "Pattern type" : "Comma-separated values"}<input value={values} onChange={(event) => setValues(event.target.value)} placeholder={type === "type" ? "integer, number, or date" : type === "pattern" ? "email, phone, blood_pressure, or regex" : "Value A, Value B"}/></label>}<button className="primary-button">Confirm and validate</button></form><div><h3>Suggested assumptions</h3>{suggestions.length === 0 ? <div className="empty-small">No unconfirmed suggestions.</div> : suggestions.slice(0,12).map((item: Json) => <div className="suggestion" key={item.signature || `${item.file_id}-${item.name}-${item.rule_type}`}><div><strong>{item.name}</strong><p>{item.filename} · {item.reason}</p></div><button onClick={() => confirmSuggestion(item)}>Confirm</button></div>)}</div></div><div className="section-heading rules-heading"><h3>Confirmed assumptions</h3><span>{confirmedRules.length} active{disabledCount ? ` · ${disabledCount} disabled hidden` : ""}</span></div>{confirmedRules.length === 0 ? <div className="empty-small">No confirmed study-specific rules yet.</div> : <div className="rule-list">{confirmedRules.map((rule: Json) => <div key={rule.id}><span className="status-dot"></span><div><strong>{rule.name}</strong><small>{rule.rule_type.replaceAll("_", " ")} · active</small></div><button onClick={() => disable(rule.id)} title="Disable this rule">Disable</button></div>)}</div>}</section>;
}

function RuleMaintenance({ projectId, files, rules, refresh, setNotice, setError }: any) {
  const confirmed = rules.filter((rule: Json) => rule.status === "confirmed" && rule.rule_type !== "cross_column");
  const [ruleId, setRuleId] = useState(confirmed[0]?.id || "");
  const active = confirmed.find((rule: Json) => rule.id === ruleId) || confirmed[0];
  const [name, setName] = useState(active?.name || "");
  const [column, setColumn] = useState(active?.parameters?.column || "");
  const [minimum, setMinimum] = useState(active?.parameters?.minimum ?? "");
  const [maximum, setMaximum] = useState(active?.parameters?.maximum ?? "");
  const [values, setValues] = useState("");
  const file = files.find((item: Json) => item.id === active?.file_id);
  const columns = file?.profile?.columns || [];
  useEffect(() => {
    if (!active) return;
    setRuleId(active.id); setName(active.name); setColumn(active.parameters?.column || "");
    setMinimum(active.parameters?.minimum ?? ""); setMaximum(active.parameters?.maximum ?? "");
    setValues((active.parameters?.values || active.parameters?.codes || [active.parameters?.expected].filter(Boolean)).join(", "));
  }, [active?.id]);
  async function save(event: FormEvent) {
    event.preventDefault(); if (!active) return;
    const parameters = { ...active.parameters, column };
    if (active.rule_type === "range") { parameters.minimum = minimum === "" ? null : Number(minimum); parameters.maximum = maximum === "" ? null : Number(maximum); }
    if (active.rule_type === "allowed_values") parameters.values = values.split(",").map((item) => item.trim()).filter(Boolean);
    if (active.rule_type === "missing_codes") parameters.codes = values.split(",").map((item) => item.trim()).filter(Boolean);
    if (["type", "date"].includes(active.rule_type)) parameters.expected = values.trim() || (active.rule_type === "date" ? "date" : "number");
    try { const result = await api(`/api/projects/${projectId}/rules/${active.id}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ name, parameters }) }); setNotice(`Rule updated and revalidated. ${result.finding_count} violations detected.`); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Rule update failed"); }
  }
  async function revalidate() {
    try { const result = await api(`/api/projects/${projectId}/rules/revalidate`, { method: "POST" }); setNotice(`${result.rule_count} rules revalidated; ${result.finding_count} current violations.`); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Revalidation failed"); }
  }
  if (!confirmed.length) return null;
  return <section className="advanced-rules"><div className="maintenance-title"><h3 className="section-heading">Edit and revalidate rules</h3><button onClick={revalidate}>Revalidate all confirmed rules</button></div><form className="rule-form maintenance-form" onSubmit={save}><label>Rule<select value={active?.id || ""} onChange={(event) => setRuleId(event.target.value)}>{confirmed.map((rule: Json) => <option key={rule.id} value={rule.id}>{rule.name}</option>)}</select></label><label>Rule name<input required value={name} onChange={(event) => setName(event.target.value)} /></label><label>Column<select value={column} onChange={(event) => setColumn(event.target.value)}>{columns.map((item: Json) => <option key={item.name}>{item.name}</option>)}</select></label>{active?.rule_type === "range" && <div className="inline-fields"><label>Minimum<input type="number" value={minimum} onChange={(event) => setMinimum(event.target.value)} /></label><label>Maximum<input type="number" value={maximum} onChange={(event) => setMaximum(event.target.value)} /></label></div>}{["allowed_values", "missing_codes", "type", "date"].includes(active?.rule_type) && <label>{active.rule_type === "allowed_values" ? "Allowed values" : active.rule_type === "missing_codes" ? "Missing codes" : "Expected type"}<input value={values} onChange={(event) => setValues(event.target.value)} /></label>}<button className="primary-button" type="submit">Save rule changes</button></form></section>;
}

function AdvancedRules({ projectId, files, relationships, refresh, setNotice, setError }: any) {
  const [crossFile, setCrossFile] = useState(files[0]?.id || "");
  const [whenColumn, setWhenColumn] = useState("");
  const [whenEquals, setWhenEquals] = useState("");
  const [thenColumn, setThenColumn] = useState("");
  const [thenEquals, setThenEquals] = useState("");
  const [leftFile, setLeftFile] = useState(files[0]?.id || "");
  const [rightFile, setRightFile] = useState(files[1]?.id || files[0]?.id || "");
  const [leftColumn, setLeftColumn] = useState("");
  const [rightColumn, setRightColumn] = useState("");
  const columnsFor = (fileId: string) => files.find((item: Json) => item.id === fileId)?.profile?.columns || [];

  async function addCrossRule(event: FormEvent) {
    event.preventDefault();
    try {
      const result = await api(`/api/projects/${projectId}/rules`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ file_id: crossFile, name: `${whenColumn} controls ${thenColumn}`, rule_type: "cross_column", parameters: { when_column: whenColumn, when_equals: whenEquals, then_column: thenColumn, then_equals: thenEquals === "" ? null : thenEquals } }) });
      setNotice(`Cross-column rule confirmed. ${result.finding_count} violations detected.`); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Cross-column rule failed"); }
  }

  async function addRelationship(event: FormEvent) {
    event.preventDefault();
    try {
      await api(`/api/projects/${projectId}/relationships`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ left_file_id: leftFile, left_column: leftColumn, right_file_id: rightFile, right_column: rightColumn, cardinality: "many_to_one" }) });
      setNotice("Cross-file relationship confirmed and checked."); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Relationship check failed"); }
  }

  if (!files.length) return null;
  return <section className="advanced-rules"><h3 className="section-heading">Study logic and cross-file checks</h3><div className="rules-grid"><form className="rule-form" onSubmit={addCrossRule}><h3>Cross-column condition</h3><label>Dataset<select value={crossFile} onChange={(event) => { setCrossFile(event.target.value); setWhenColumn(""); setThenColumn(""); }}>{files.map((file: Json) => <option value={file.id} key={file.id}>{file.filename}</option>)}</select></label><label>When column<select required value={whenColumn} onChange={(event) => setWhenColumn(event.target.value)}><option value="">Choose a column</option>{columnsFor(crossFile).map((item: Json) => <option key={item.name}>{item.name}</option>)}</select></label><label>Equals<input required value={whenEquals} onChange={(event) => setWhenEquals(event.target.value)} /></label><label>Then column<select required value={thenColumn} onChange={(event) => setThenColumn(event.target.value)}><option value="">Choose a column</option>{columnsFor(crossFile).map((item: Json) => <option key={item.name}>{item.name}</option>)}</select></label><label>Must equal <span>(blank means required)</span><input value={thenEquals} onChange={(event) => setThenEquals(event.target.value)} /></label><button className="primary-button" type="submit">Confirm condition</button></form><form className="rule-form" onSubmit={addRelationship}><h3>Cross-file relationship</h3>{files.length < 2 && <p className="form-hint">Upload at least two datasets to enable this check.</p>}<label>Dataset containing keys<select value={leftFile} onChange={(event) => { setLeftFile(event.target.value); setLeftColumn(""); }}>{files.map((file: Json) => <option value={file.id} key={file.id}>{file.filename}</option>)}</select></label><label>Key column<select required value={leftColumn} onChange={(event) => setLeftColumn(event.target.value)}><option value="">Choose a column</option>{columnsFor(leftFile).map((item: Json) => <option key={item.name}>{item.name}</option>)}</select></label><label>Reference dataset<select value={rightFile} onChange={(event) => { setRightFile(event.target.value); setRightColumn(""); }}>{files.map((file: Json) => <option value={file.id} key={file.id}>{file.filename}</option>)}</select></label><label>Reference key<select required value={rightColumn} onChange={(event) => setRightColumn(event.target.value)}><option value="">Choose a column</option>{columnsFor(rightFile).map((item: Json) => <option key={item.name}>{item.name}</option>)}</select></label><button className="primary-button" type="submit" disabled={files.length < 2} title={files.length < 2 ? "Upload a second dataset first" : "Confirm and check this relationship"}>Confirm relationship</button></form></div>{relationships.length > 0 && <div className="rule-list">{relationships.map((item: Json) => <div key={item.id}><span className="status-dot"></span><div><strong>{item.left_column} → {item.right_column}</strong><small>{item.cardinality.replaceAll("_", " ")} · {item.status}</small></div></div>)}</div>}</section>;
}

function GeminiAssistant({ projectId, files, setNotice, setError }: any) {
  const [status, setStatus] = useState<Json | null>(null);
  const [question, setQuestion] = useState("");
  const [fileId, setFileId] = useState(files[0]?.id || "");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [messages, setMessages] = useState<Json[]>([]);
  useEffect(() => { api("/api/assistant/status").then(setStatus).catch(() => setStatus({ configured: false })); }, []);
  async function testConnection() {
    setBusy(true); setError(""); setStatus((current: Json | null) => ({ ...current, state: "validating" }));
    try { const result = await api("/api/assistant/test", { method: "POST" }); setStatus(result); setNotice(`Gemini connection verified with ${result.model}.`); }
    catch (reason) { setStatus((current: Json | null) => ({ ...current, state: "connection_failed" })); setError(reason instanceof Error ? reason.message : "Gemini connection test failed"); }
    finally { setBusy(false); }
  }
  async function ask(event: FormEvent) {
    event.preventDefault(); if (!question.trim()) return;
    const userQuestion = question.trim(); setQuestion(""); setBusy(true); setMessages((current) => [...current, { role: "user", text: userQuestion }]);
    try {
      const result = await api(`/api/projects/${projectId}/assistant/propose`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ question: userQuestion, file_id: fileId || null, consent_to_send_data: consent }) });
      setMessages((current) => [...current, { role: "assistant", text: result.answer, sources: result.sources, proposal_count: result.proposal_count, model: result.model }]);
      setNotice(result.proposal_count ? `${result.proposal_count} Gemini proposal(s) added to Issues for review.` : "Gemini answered without creating a correction proposal.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Gemini request failed"); }
    finally { setBusy(false); }
  }
  const ready = status?.state === "ready";
  return <section className="assistant-card"><div className="assistant-heading"><div><p className="eyebrow">OPTIONAL CUSTOM ASSISTANT</p><h2>Ask Scribe with Gemini</h2><p>Gemini inspects samples and proposes reviewable rules or corrections. Deterministic local validation remains authoritative.</p></div><div className="connection-control"><span className={ready ? "configured" : "not-configured"}>{status?.state === "validating" ? "Validating…" : ready ? `${status.model} verified` : status?.configured ? "Configured — connection unverified" : "API key not configured"}</span>{status?.configured && <button type="button" disabled={busy} onClick={testConnection}>Test connection</button>}</div></div>{!status?.configured && <div className="assistant-setup"><strong>To enable Gemini</strong><p>Add <code>GEMINI_API_KEY=your_key</code> to Scribe’s <code>.env</code> file, optionally set <code>GEMINI_MODEL</code>, then restart. The key stays in the Python service and is never sent to the browser or stored in the project.</p></div>}<div className="chat-log" aria-live="polite">{messages.length === 0 ? <div className="chat-empty">Examples: “Suggest a rule for this medication field” or “Flag impossible combinations in these columns.”</div> : messages.map((message, index) => <div key={`${message.role}-${index}`} className={`chat-message ${message.role}`}><strong>{message.role === "user" ? "You" : "Scribe + Gemini"}</strong><p>{message.text}</p>{message.sources?.length > 0 && <div className="chat-sources">{message.sources.map((source: Json) => <a href={`/projects/${projectId}/issues`} key={source.finding_id}>{source.filename} · {source.column} · row {source.row_number}</a>)}</div>}{message.proposal_count != null && <small>{message.proposal_count} reviewable proposal(s) · {message.model} · sampled data only</small>}</div>)}</div><form className="chat-form" onSubmit={ask}><label>Dataset context<select value={fileId} onChange={(event) => setFileId(event.target.value)}><option value="">Up to three project datasets</option>{files.map((file: Json) => <option key={file.id} value={file.id}>{file.filename}</option>)}</select></label><label>Your request<textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Describe the issue or rule you want Scribe to investigate…" /></label><label className="consent-check"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /><span>I understand that profiles and up to 25 sample rows from the selected dataset will be sent to Google Gemini for this request.</span></label><button className="primary-button" type="submit" disabled={!ready || !consent || busy || !question.trim()} title={!ready ? "Test the configured Gemini connection first" : !consent ? "Confirm data-sharing consent first" : "Send this custom QA request"}>{busy ? "Checking…" : "Ask Gemini"}</button></form></section>;
}

function BatchReview({ findings, batchDecide, busy }: any) {
  const safe = findings.filter((item: Json) => item.status === "pending" && item.operation && item.confidence === "high" && item.operation.type !== "delete_rows");
  const [open, setOpen] = useState(false);
  const affected = safe.reduce((total: number, item: Json) => total + (item.affected_count || 1), 0);
  const categories = [...new Set(safe.map((item: Json) => item.category.replaceAll("_", " ")))];
  return <><div className="batch-review"><div><strong>Batch review</strong><span>Preview exact, high-confidence transformations before applying them.</span></div><button disabled={!safe.length || busy} title={safe.length ? `Preview ${safe.length} grouped transformations` : "No safe pending corrections are available"} onClick={() => setOpen(true)}>{busy ? "Applying corrections…" : `Review ${safe.length} safe transformations`}</button></div>{open && <div className="modal-backdrop" role="presentation" onClick={() => setOpen(false)}><div className="modal batch-modal" role="dialog" aria-modal="true" aria-labelledby="batch-title" onClick={(event) => event.stopPropagation()}><button className="modal-close" aria-label="Close batch preview" onClick={() => setOpen(false)}>×</button><p className="eyebrow">BATCH PREVIEW</p><h2 id="batch-title">Apply {safe.length} grouped transformations?</h2><p>This will change {affected.toLocaleString()} referenced cell(s) in reviewed copies only. Originals remain unchanged.</p><dl><dt>Checks</dt><dd>{categories.join(", ")}</dd><dt>Files</dt><dd>{new Set(safe.map((item: Json) => item.file_id)).size}</dd><dt>Conflicts</dt><dd>Preflight validation runs before any decision is saved.</dd></dl><div className="modal-actions"><button onClick={() => setOpen(false)}>Cancel</button><button className="primary-button" disabled={busy} onClick={async () => { await batchDecide(safe.map((item: Json) => item.id), "accepted"); setOpen(false); }}>Accept reviewed changes</button></div></div></div>}</>;
}

function reviewOnlyGuidance(finding: Json) {
  const column = finding.column_name ? ` in ${finding.column_name}` : "";
  const common = {
    problem: `Scribe found ${finding.affected_count || 1} row(s) that need research judgment${column}.`,
    risk: "Changing these values automatically could invent data, hide missingness, or remove valid participants without evidence.",
    strategies: ["Leave the source value unchanged and document the limitation.", "Use a confirmed codebook or questionnaire rule before changing values.", "Exclude rows or variables later only if your analysis plan justifies it."],
    acknowledge: "Issue is real, but no defensible automatic correction is available. Preserve the value as-is and document this limitation for analysis.",
    falsePositive: "The flagged value is valid for this study based on the instrument/codebook; no cleaning action is needed.",
    defer: "Needs codebook, PI, or study-instrument review before a cleaning decision can be made.",
  };
  const byCategory: Record<string, Partial<typeof common>> = {
    missing_value: { problem: `Scribe found missing or blank value(s)${column}.`, risk: "Missing responses can bias analysis if filled without evidence.", strategies: ["Keep true missing values blank/NA for analysis.", "Normalize confirmed missing codes only after checking the codebook.", "Consider row, column, or complete-case exclusion in the analysis plan, not as an automatic data edit."], acknowledge: "Value is genuinely missing and no evidence-backed replacement exists. Preserve as missing and handle with the analysis plan." },
    identity_not_verifiable: { problem: "Scribe could not find a trustworthy participant identifier.", risk: "Names, emails, or repeated labels can point to people but are not always stable participant keys.", strategies: ["Confirm the intended participant ID column from the study materials.", "Avoid treating names as unique identifiers unless explicitly justified.", "Document that duplicate/person-level checks are limited until an ID is configured."], acknowledge: "No reliable participant identifier was collected or configured. Person-level duplicate checks are limited and this limitation is documented." },
    near_duplicate: { problem: "Scribe found records that look similar but are not exact duplicates.", risk: "Automatically removing near-duplicates can delete valid repeated measures, follow-ups, or household members.", strategies: ["Compare participant key, visit date/time, and study arm before deciding.", "Keep all rows when repeated participation is plausible.", "Remove records only after explicit survivorship review."], acknowledge: "Possible duplicate was reviewed, but available evidence is insufficient for deletion. Preserve all rows." },
    potential_duplicate: { problem: "Scribe found records that may describe the same participant or event.", risk: "Potential duplicates require survivorship judgment and can overlap with valid repeated observations.", strategies: ["Use configured participant keys and visit rules.", "Choose a retained record explicitly before deletion.", "Defer if the study permits repeated entries or follow-up visits."], acknowledge: "Potential duplicate was reviewed, but no defensible survivorship decision can be made from the available evidence. Preserve all rows." },
    ambiguous_date: { problem: `Scribe found date value(s)${column} that can be interpreted more than one way.`, risk: "Guessing date locale can shift visits, eligibility windows, and longitudinal timing.", strategies: ["Confirm date locale and collection format.", "Keep ambiguous dates unchanged until locale is configured.", "Standardize only unambiguous dates or dates backed by metadata."], acknowledge: "Date is ambiguous without locale metadata. Preserve the original token and document that date-based analysis needs confirmation." },
    outlier: { problem: `Scribe found statistically unusual value(s)${column}.`, risk: "Outliers can be true observations; automatic replacement or deletion can bias estimates.", strategies: ["Check units, instrument range, and source records.", "Winsorize, transform, or exclude only in the analysis plan.", "Leave valid extremes unchanged and document sensitivity checks."], acknowledge: "Outlier is plausible or cannot be disproven from source evidence. Preserve value and handle through documented analysis sensitivity checks." },
    arithmetic_relationship_unverified: { problem: "Scribe found a value that may be derivable, but the relationship is not confirmed.", risk: "A guessed formula can manufacture data or mask a valid exception.", strategies: ["Confirm the formula from the questionnaire or data dictionary.", "Use evidence-backed manual correction only when row inputs prove the value.", "Otherwise preserve missing/unclean values and document the limitation."], acknowledge: "No confirmed formula or row-level evidence supports a replacement. Preserve the value and document the limitation." },
    ai_custom: { problem: "Gemini proposed an issue that still needs local researcher review.", risk: "Assistant suggestions are not authoritative and may misread study context.", strategies: ["Check the proposed issue against the source rows and codebook.", "Accept only evidence-backed deterministic edits.", "Use acknowledgement or false-positive review for non-editable observations."], acknowledge: "External assistant suggestion was reviewed as a limitation; no deterministic correction is safe from the available evidence." },
  };
  return { ...common, ...(byCategory[finding.category] || {}) };
}

function ReviewOnlyPanel({ active, rationale, setRationale, disposition, busy }: any) {
  const guidance = reviewOnlyGuidance(active);
  const actions = [
    { value: "acknowledged", title: "Acknowledge limitation", body: "The issue is real, but the safest cleaning strategy is to preserve the value and document how analysis should handle it.", rationale: guidance.acknowledge, primary: true },
    { value: "false_positive", title: "Mark false positive", body: "The flagged value is valid for this study or the detector misunderstood the context.", rationale: guidance.falsePositive },
    { value: "deferred", title: "Defer decision", body: "The finding needs a codebook, PI, instrument, or analysis-plan decision before it can be closed.", rationale: guidance.defer },
  ];
  return <div className="review-only-panel"><div className="review-only-heading"><span>Needs confirmation</span><h3>{guidance.problem}</h3><p>{guidance.risk}</p></div><div className="strategy-card"><strong>Recommended cleaning strategies</strong><ul>{guidance.strategies.map((strategy: string) => <li key={strategy}>{strategy}</li>)}</ul></div><label className="rationale-field">Decision note<textarea value={rationale} onChange={(event) => setRationale(event.target.value)} placeholder="Optional: replace the suggested note with study-specific evidence." /></label><div className="review-actions">{actions.map((action) => <button key={action.value} type="button" className={action.primary ? "primary-button review-action" : "review-action"} disabled={busy} onClick={() => disposition(action.value, rationale.trim() || action.rationale)}><strong>{action.title}</strong><span>{action.body}</span><small>Save this review decision</small></button>)}</div></div>;
}

function ManualOperationPanel({ active, submit, busy }: any) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<"cell_correction" | "exclude_row" | "exclude_column">("cell_correction");
  const [value, setValue] = useState("");
  const [evidence, setEvidence] = useState("");
  const [rationale, setRationale] = useState("");
  useEffect(() => { setOpen(false); setKind("cell_correction"); setValue(""); setEvidence(""); setRationale(""); }, [active?.id]);
  if (!active?.row_id && !active?.column_name) return null;
  const canSubmit = evidence.trim().length >= 3 && rationale.trim().length >= 3 && (kind !== "cell_correction" || value !== "");
  return <div className="manual-operation"><button type="button" onClick={() => setOpen((current) => !current)}>{open ? "Cancel evidence-backed operation" : "Use source evidence to correct or exclude"}</button>{open && <div className="manual-operation-form"><label>Operation<select value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="cell_correction" disabled={!active.row_id || !active.column_name}>Correct this cell</option><option value="exclude_row" disabled={!active.row_id}>Exclude this row</option><option value="exclude_column" disabled={!active.column_name}>Exclude this column</option></select></label>{kind === "cell_correction" && <label>Confirmed replacement<input value={value} onChange={(event) => setValue(event.target.value)} placeholder="Enter the value shown in the source record" /></label>}<label>Evidence<textarea value={evidence} onChange={(event) => setEvidence(event.target.value)} placeholder="Example: Codebook version 2 defines -9 as missing." /></label><label>Analysis rationale<textarea value={rationale} onChange={(event) => setRationale(event.target.value)} placeholder="Explain why this change or exclusion is justified." /></label><button className="primary-button" disabled={busy || !canSubmit} onClick={() => submit({ kind, row_id: active.row_id, column: active.column_name, before: active.before, after: kind === "cell_correction" ? value : null, evidence, rationale })}>{kind === "cell_correction" ? "Apply documented correction" : kind === "exclude_row" ? "Exclude documented row" : "Exclude documented column"}</button><small>This creates a separate audited operation with exact preconditions. It never edits the original upload.</small></div>}</div>;
}

function IssuesView({ findings, total, page, setPage, status, setStatus, active, preview, select, decide, disposition, manualOperation, undo, detailsOpen, closeDetails, openDetails, busy }: any) {
  const [edit, setEdit] = useState(false); const [value, setValue] = useState(""); const [rationale, setRationale] = useState(""); const [retainedRowId, setRetainedRowId] = useState("");
  const derived = active?.operation && ["derive_mapping", "derive_arithmetic"].includes(active.operation.type);
  const duplicateDeletion = active?.operation?.type === "delete_rows";
  const duplicateRows = duplicateDeletion ? [{ rowId: active.row_id, row: active.row_number }, ...(active.operation.row_ids || []).map((rowId: string, index: number) => ({ rowId, row: active.operation.rows?.[index] }))].filter((item: Json) => item.rowId) : [];
  const pageSize = 30; const pages = Math.max(1, Math.ceil(total / pageSize)); const pageItems = findings;
  useEffect(() => { setEdit(false); setRationale(""); setValue(active?.proposed == null ? "" : String(active.proposed)); setRetainedRowId(active?.row_id || ""); }, [active?.id, active?.proposed]);
  if (!findings.length) return <Empty title="No findings yet" body="Upload a dataset or confirm validation rules. Scribe will show evidence here."/>;
  return <section><div className="page-title"><div><p className="eyebrow">GUIDED REVIEW</p><h2>Detected issues</h2><p>Review grouped evidence and decide every change. Originals never change.</p></div><label className="filter">Show<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All findings</option><option value="pending">Pending</option><option value="accepted">Accepted</option><option value="acknowledged">Acknowledged</option><option value="rejected">Rejected</option></select></label></div><div className={`issues-layout ${detailsOpen ? "" : "details-closed"}`}><aside className="issue-list">{pageItems.map((item: Json) => <button key={item.id} className={active?.id === item.id ? "selected" : ""} onClick={() => { select(item.id); openDetails(); }}><span className={`severity-dot ${item.severity}`}></span><div><strong>{item.title}</strong><small>{item.filename} · {item.column_name || "Dataset"} · {item.affected_count} affected</small></div><span className={`status-pill ${item.status}`}>{item.status}</span></button>)}<div className="pagination"><button disabled={page === 0} onClick={() => setPage((current) => current - 1)}>Previous</button><span>{total ? `${page + 1} / ${pages} · ${total.toLocaleString()} findings` : "No matches"}</span><button disabled={page + 1 >= pages} onClick={() => setPage((current) => current + 1)}>Next</button></div></aside>{active && <article className="finding-detail"><div className="finding-top"><div><span className={`badge ${active.severity}`}>{active.severity}</span><span className="badge confidence">{active.confidence.replaceAll("_", " ")}</span><h2>{active.title}</h2></div>{!detailsOpen && <button onClick={openDetails}>Show evidence</button>}</div><a className="source-link" href={`/projects/${active.project_id}/files`}>{active.filename} · {active.table_name} · {active.column_name || "Dataset"} · Row {active.row_number || "—"}</a><p>{active.explanation}</p>{derived && <div className="unchanged-card"><strong>Verified formula: {active.operation.formula}</strong><p>Each of the {active.affected_count.toLocaleString()} rows has its own recorded inputs, original token, and calculated result. Ambiguous rows are excluded.</p></div>}<div className="before-after"><div><span>Representative detected value</span><code>{JSON.stringify(active.before)}</code></div><div><span>{active.operation ? "Representative proposed result" : "Cleaning decision"}</span>{edit ? <input value={value} onChange={(event) => setValue(event.target.value)} autoFocus/> : <code>{active.proposed == null ? "No automatic correction is safe" : JSON.stringify(active.proposed)}</code>}</div></div><FindingTable preview={preview}/>{duplicateDeletion && active.status === "pending" && <label className="rationale-field">Duplicate row to retain<select value={retainedRowId} onChange={(event) => setRetainedRowId(event.target.value)}>{duplicateRows.map((item: Json) => <option key={item.rowId} value={item.rowId}>Keep original row {item.row}</option>)}</select><small>Scribe will remove every other identical row only after you choose the retained record.</small></label>}{active.status === "pending" && !active.operation && <><ReviewOnlyPanel active={active} rationale={rationale} setRationale={setRationale} disposition={disposition} busy={busy} /><ManualOperationPanel active={active} submit={manualOperation} busy={busy} /></>}<div className="decision-bar">{active.status === "pending" ? active.operation ? duplicateDeletion ? <><button className="primary-button" disabled={busy || !retainedRowId} onClick={() => decide("accepted", undefined, { retained_row_id: retainedRowId, removed_row_ids: duplicateRows.filter((item: Json) => item.rowId !== retainedRowId).map((item: Json) => item.rowId) })}>Keep selected row and remove duplicates</button><button disabled={busy} onClick={() => decide("rejected")}>Keep all rows</button></> : <>{!edit && <button className="primary-button" disabled={busy} onClick={() => decide("accepted")}>{derived ? `Accept ${active.affected_count.toLocaleString()} proven values` : active.affected_count > 1 ? `Accept for ${active.affected_count} exact matches` : "Accept correction"}</button>}{!derived && <button disabled={busy} onClick={() => setEdit(!edit)}>{edit ? "Cancel edit" : "Edit correction"}</button>}{edit && <button className="primary-button" disabled={busy || !value.trim()} onClick={() => decide("accepted", value)}>Save and accept</button>}<button disabled={busy} onClick={() => decide("rejected")}>Reject proposal</button></> : null : <button disabled={busy} onClick={undo}>Undo {active.status} decision</button>}</div></article>}{detailsOpen && active && <aside className="evidence-panel"><button className="panel-close" aria-label="Close evidence panel" onClick={closeDetails}>×</button><h3>Evidence & details</h3><dl><dt>Finding fingerprint</dt><dd>{active.fingerprint || active.id}</dd><dt>Check</dt><dd>{active.category.replaceAll("_", " ")}</dd><dt>Detector</dt><dd>{active.detector_version || "legacy"}</dd><dt>Affected</dt><dd>{active.affected_count} row(s)</dd>{derived && <><dt>Formula</dt><dd>{active.operation.formula}</dd><dt>Evidence columns</dt><dd>{active.operation.source_columns.join(", ")}</dd></>}<dt>Disposition</dt><dd>{active.disposition || active.status}</dd></dl><div className="unchanged-card"><strong>Original unchanged</strong><p>Accepted corrections rebuild a separate reviewed version from the immutable source. Review-only findings record a decision note without changing data.</p></div></aside>}</div></section>;
}

function ExportsView({ exports, createExport, readiness, rStatus }: any) {
  const unresolved = Object.values(readiness.unresolved_by_severity || {}).reduce((sum: number, count: any) => sum + Number(count), 0);
  const blockingChecks = (readiness.checklist || []).filter((item: Json) => item.number < 17 && ["attention", "blocked", "failed"].includes(item.status));
  const blockers = [
    ...(readiness.needs_rescan ? ["Run the current scan against the reviewed data."] : []),
    ...(unresolved > 0 ? [`Review or acknowledge ${unresolved.toLocaleString()} remaining finding(s).`] : []),
    ...blockingChecks.map((item: Json) => item.number === 16 && item.evidence?.some((evidence: Json) => evidence.r_runtime_available === false)
      ? "Install R so the local Rscript command can reproduce the cleaned data."
      : `${item.name}: ${item.status.replaceAll("_", " ")}.`),
  ];
  const verifiedBlocked = blockers.length > 0;
  return <section><div className="page-title"><div><p className="eyebrow">REPRODUCIBLE OUTPUTS</p><h2>Exports</h2><p>Create a provisional review package at any time. A verified clean export requires every final gate, including R reproduction.</p></div><div className="export-actions"><button onClick={() => createExport("review")}>Generate review package</button><button className="primary-button" disabled={verifiedBlocked} title={verifiedBlocked ? blockers[0] : "Generate and verify the clean export with R"} onClick={() => createExport("verified")}>Generate verified clean export</button></div></div>{rStatus && <div className={rStatus.ready ? "runtime-status ready" : "runtime-status blocked"}><strong>{rStatus.ready ? "R verification is ready" : "R verification needs setup"}</strong><p>{rStatus.message}</p>{!rStatus.available && <small>Install R from r-project.org, restart Scribe, then install packages with: <code>install.packages(c(&quot;readr&quot;, &quot;dplyr&quot;, &quot;openxlsx&quot;, &quot;haven&quot;))</code></small>}{rStatus.available && rStatus.version && <small>{rStatus.version}</small>}</div>}{verifiedBlocked && <div className="export-gate"><strong>Verified export is blocked</strong><p>Status: {readiness.status.replaceAll("_", " ")}. A review package is available and clearly marked provisional.</p><ul>{blockers.map((blocker: string) => <li key={blocker}>{blocker}</li>)}</ul></div>}{exports.length === 0 ? <Empty title="No exports yet" body="Generate a provisional review package to inspect the cleaned copies, scripts, audit trail, findings, hashes, and readiness evidence."/> : <div className="export-list">{exports.map((item: Json) => <article key={item.id}><div><span className={`status-pill ${item.status}`}>{item.status}</span><span className={`export-kind ${item.kind}`}>{item.kind === "verified" ? "Verified clean export" : "Provisional review package"}</span><strong>{new Date(item.created_at).toLocaleString()}</strong><small>{item.validation?.r_reproduced ? "R reproduction passed" : item.kind === "review" ? "R reproduction not verified" : "Validation incomplete"}</small>{item.error && <p className="form-error">{item.error}</p>}</div>{item.status === "complete" && <a className="primary-button" href={`${API}/api/exports/${item.id}/download`}>Download ZIP</a>}<div className="artifacts">{item.artifacts?.filter((artifact: Json) => artifact.kind !== "bundle").map((artifact: Json) => <a key={artifact.id} href={`${API}/api/exports/${item.id}/artifacts/${artifact.id}`}>{artifact.filename}<small>{artifact.kind.replaceAll("_", " ")} · {formatBytes(artifact.size_bytes)}</small></a>)}</div></article>)}</div>}</section>;
}

function DataTable({ preview }: { preview: Json | null }) { if (!preview) return <div className="preview-placeholder">Select a dataset to preview it.</div>; return <div className="data-preview"><div className="preview-title"><strong>{preview.filename}</strong><span>{preview.version} · {preview.total.toLocaleString()} rows</span></div><div className="table-scroll"><table><thead><tr><th>Row</th>{preview.columns.map((column: string) => <th key={column}>{column}</th>)}</tr></thead><tbody>{preview.rows.map((row: Json) => <tr key={row.row_number}><td>{row.row_number}</td>{preview.columns.map((column: string) => <td key={column}>{row.values[column]}</td>)}</tr>)}</tbody></table></div></div>; }
function FindingTable({ preview }: { preview: Json | null }) { if (!preview) return <div className="preview-placeholder compact">Loading source rows…</div>; return <div className="table-scroll finding-table"><table><thead><tr><th>Row</th>{preview.columns.slice(0,5).map((column: string) => <th key={column}>{column}</th>)}</tr></thead><tbody>{preview.original_rows.map((row: Json, index: number) => <tr key={row.row_number}><td>{row.row_number}</td>{preview.columns.slice(0,5).map((column: string) => <td key={column} className={row.values[column] !== preview.reviewed_rows[index]?.values[column] ? "changed" : ""}>{row.values[column] !== preview.reviewed_rows[index]?.values[column] ? `${row.values[column]} → ${preview.reviewed_rows[index]?.values[column]}` : row.values[column]}</td>)}</tr>)}</tbody></table></div>; }
function Empty({ title, body, action, onAction }: { title: string; body: string; action?: string; onAction?: () => void }) { return <div className="empty-state"><span>□</span><h3>{title}</h3><p>{body}</p>{action && onAction && <button className="primary-button" onClick={onAction}>{action}</button>}</div>; }
function Centered({ title, body }: { title: string; body: string }) { return <main className="centered"><div className="brand large"><span className="brand-mark">S</span><span>Scribe</span></div><h1>{title}</h1><p>{body}</p></main>; }
function ConnectionError({ message, retry }: { message: string; retry: () => void }) { return <main className="centered error-page"><div className="brand large"><span className="brand-mark">S</span><span>Scribe</span></div><h1>Scribe’s local service is unavailable</h1><p>{message}</p><p>Start Scribe with its launcher, then retry. No sample data has been substituted.</p><button className="primary-button" onClick={retry}>Retry connection</button></main>; }
