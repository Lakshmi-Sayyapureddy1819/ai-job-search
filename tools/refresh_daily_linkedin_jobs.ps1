$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Cli = Join-Path $Repo '.agents\skills\linkedin-search\cli\src\cli.ts'
$Loader = ([System.Uri]::new((Resolve-Path (Join-Path $Repo 'tools\linkedin_ts_loader.mjs')).Path)).AbsoluteUri
$ReportDir = Join-Path $Repo 'job_scraper\daily_jobs'
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

$Candidates = @(
    @{
        Name = 'Lakshmi'
        Max = 100
        ExperienceNote = 'Lakshmi has 1+ year of combined internship and full-time experience, including 8 months full-time. Verify role requirements against her profile.'
        Searches = @(
            @{ Query = 'AI Engineer'; Location = 'India'; Remote = $true }
            @{ Query = 'Generative AI Engineer'; Location = 'India'; Remote = $true }
            @{ Query = 'Machine Learning Engineer'; Location = 'India'; Remote = $true }
            @{ Query = 'Python Developer'; Location = 'India'; Remote = $true }
            @{ Query = 'Backend Developer'; Location = 'India'; Remote = $true }
            @{ Query = 'LLM Engineer'; Location = 'India'; Remote = $true }
            @{ Query = 'Applied AI Engineer'; Location = 'India'; Remote = $true }
            @{ Query = 'AI Developer'; Location = 'India'; Remote = $true }
            @{ Query = 'Software AI Engineer'; Location = 'India'; Remote = $true }
            @{ Query = 'ML Engineer'; Location = 'India'; Remote = $true }
            @{ Query = 'AI Application Engineer'; Location = 'India'; Remote = $true }
            @{ Query = 'NLP Engineer'; Location = 'India'; Remote = $true }
            @{ Query = 'Data Scientist AI'; Location = 'India'; Remote = $true }
            @{ Query = 'Python Machine Learning Engineer'; Location = 'India'; Remote = $true }
        )
    }
    @{
        Name = 'Surya'
        Max = 100
        ExperienceNote = 'Surya has 1.5 years of experience. Verify role requirements against his profile.'
        Searches = @(
            foreach ($City in @('Hyderabad, Telangana, India', 'Visakhapatnam, Andhra Pradesh, India', 'Bengaluru, Karnataka, India', 'Chennai, Tamil Nadu, India')) {
                foreach ($Role in @('Software Engineer', 'Forward Deployed Engineer', 'Full Stack Engineer')) {
                    @{ Query = $Role; Location = $City; Remote = $false }
                }
            }
        )
    }
)

$ProfilePath = Join-Path $Repo 'job_scraper\web_candidate_profiles.json'
if (Test-Path -LiteralPath $ProfilePath) {
    try {
        $SavedProfiles = Get-Content -LiteralPath $ProfilePath -Raw | ConvertFrom-Json
        foreach ($candidate in $Candidates) {
            $saved = $SavedProfiles.PSObject.Properties[$candidate.Name].Value
            if (-not $saved) { continue }
            if ($saved.target_per_day -is [int] -and $saved.target_per_day -ge 1 -and $saved.target_per_day -le 100) { $candidate.Max = $saved.target_per_day }
            if ($saved.experience) { $candidate.ExperienceNote = "$($candidate.Name) has $($saved.experience). Verify role requirements against the profile." }
            if ($saved.roles -and $saved.locations) {
                $locations = @($saved.locations | Where-Object { $_ -and $_ -ne 'Remote' })
                if ($candidate.Name -eq 'Lakshmi' -and $locations -contains 'India') { $locations = @('India') }
                $searches = [System.Collections.Generic.List[object]]::new()
                foreach ($location in $locations) {
                    foreach ($role in $saved.roles) {
                        $pages = if ($candidate.Name -eq 'Lakshmi') { 2 } else { 1 }
                        for ($page = 1; $page -le $pages; $page++) {
                            $searches.Add(@{ Query = [string]$role; Location = [string]$location; Remote = ($candidate.Name -eq 'Lakshmi'); Page = $page })
                        }
                    }
                }
                if ($searches.Count -gt 0) { $candidate.Searches = @($searches.ToArray()) }
            }
        }
    } catch {
        Add-Content -LiteralPath (Join-Path $Repo 'outputs\daily-job-refresh.log') -Value "$(Get-Date -Format o) Profile preferences could not be loaded: $($_.Exception.Message)"
    }
}

function Get-LinkedInJobs($Candidate, $SeenUrls) {
    $byUrl = [System.Collections.Generic.Dictionary[string, object]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($search in $Candidate.Searches) {
        $page = if ($search.Page) { $search.Page } else { 1 }
        $args = @('--no-warnings', '--experimental-loader', $Loader, $Cli, 'search', '--query', $search.Query,
            '--location', $search.Location, '--jobage', '1', '--page', [string]$page, '--limit', '10', '--format', 'json')
        if ($search.Remote) { $args += @('--remote', 'remote') }
        try {
            $raw = (& node @args 2>$null | Out-String).Trim()
            if ($LASTEXITCODE -ne 0 -or -not $raw) { continue }
            $payload = $raw | ConvertFrom-Json
            foreach ($job in $payload.results) {
                if ($job.url -and -not $SeenUrls.Contains($job.url) -and -not $byUrl.ContainsKey($job.url)) {
                    $byUrl[$job.url] = $job
                }
            }
        } catch {
            Add-Content -LiteralPath (Join-Path $Repo 'outputs\daily-job-refresh.log') -Value "$(Get-Date -Format o) $($Candidate.Name) $($search.Query) $($search.Location): $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 1
    }
    return @($byUrl.Values | Select-Object -First $Candidate.Max)
}

function Get-FreehireJobs($Candidate, $SeenUrls, $Limit) {
    $cli = Join-Path $Repo '.agents\skills\freehire-search\cli\src\cli.ts'
    if ($Limit -lt 1 -or -not (Test-Path -LiteralPath $cli)) { return @() }
    $roles = @($Candidate.Searches | Select-Object -ExpandProperty Query -Unique | Select-Object -First 5)
    if (-not $roles.Count) { return @() }
    $query = ($roles | ForEach-Object { 'title:"' + ($_ -replace '"', '') + '"' }) -join ' OR '
    $byUrl = [System.Collections.Generic.Dictionary[string, object]]::new([System.StringComparer]::OrdinalIgnoreCase)
    for ($page = 1; $page -le 4 -and $byUrl.Count -lt $Limit; $page++) {
        $args = @('--no-warnings', '--experimental-loader', $Loader, $cli, 'search', '--query', $query,
            '--country', 'IN', '--jobage', '1', '--page', [string]$page, '--limit', '25', '--no-description', '--format', 'json')
        if ($Candidate.Name -eq 'Lakshmi') { $args += @('--remote', 'remote') }
        try {
            $raw = (& node @args 2>$null | Out-String).Trim()
            if ($LASTEXITCODE -ne 0 -or -not $raw) { continue }
            $payload = $raw | ConvertFrom-Json
            foreach ($job in $payload.results) {
                $url = [string]$job.url
                if (-not $url -or $SeenUrls.Contains($url) -or $byUrl.ContainsKey($url)) { continue }
                if ($Candidate.Name -eq 'Surya') {
                    $place = [string]$job.location
                    if ($place -and $place -notmatch '(?i)Hyderabad|Visakhapatnam|Bengaluru|Bangalore|Chennai') { continue }
                }
                $job | Add-Member -NotePropertyName source -NotePropertyValue 'Freehire' -Force
                $byUrl[$url] = $job
            }
        } catch {
            Add-Content -LiteralPath (Join-Path $Repo 'outputs\daily-job-refresh.log') -Value "$(Get-Date -Format o) Freehire $($Candidate.Name) page $page`: $($_.Exception.Message)"
        }
    }
    return @($byUrl.Values | Select-Object -First $Limit)
}

function Escape-TableCell([string]$Value) {
    if (-not $Value) { return 'Not stated' }
    return ($Value -replace '\|', '\|').Replace("`r", ' ').Replace("`n", ' ').Trim()
}

$Now = [DateTimeOffset]::Now
$Stamp = $Now.ToString('yyyy-MM-dd_HH-mm')
$DisplayDate = $Now.ToString('yyyy-MM-dd HH:mm zzz')
$Summaries = [System.Collections.Generic.List[string]]::new()
$SeenByCandidate = @{
    Lakshmi = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    Surya = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
}
foreach ($oldReport in Get-ChildItem -LiteralPath $ReportDir -Filter '*.md' -File) {
    $oldText = Get-Content -LiteralPath $oldReport.FullName -Raw
    $candidateMatch = [regex]::Match($oldText, '^\*\*Candidate:\s*(Lakshmi|Surya)\*\*', 'Multiline')
    $oldCandidate = if ($candidateMatch.Success) { $candidateMatch.Groups[1].Value } else { 'Lakshmi' }
    foreach ($match in [regex]::Matches($oldText, '\]\((https?://[^)]+)\)')) {
        [void]$SeenByCandidate[$oldCandidate].Add($match.Groups[1].Value)
    }
}
foreach ($candidateName in @('Lakshmi', 'Surya')) {
    if (Test-Path -LiteralPath (Join-Path $Repo 'job_scraper\seen_jobs.json')) {
        try {
            $seenState = Get-Content -LiteralPath (Join-Path $Repo 'job_scraper\seen_jobs.json') -Raw | ConvertFrom-Json
            foreach ($property in $seenState.seen.PSObject.Properties) {
                $url = [string]$property.Value.url
                if ($url -match '^https?://') { [void]$SeenByCandidate[$candidateName].Add($url) }
            }
        } catch { }
    }
    if (Test-Path -LiteralPath (Join-Path $Repo 'job_search_tracker.csv')) {
        try {
            foreach ($tracked in Import-Csv -LiteralPath (Join-Path $Repo 'job_search_tracker.csv')) {
                $url = [string]$tracked.source
                if ($url -match '^https?://') { [void]$SeenByCandidate[$candidateName].Add($url) }
            }
        } catch { }
    }
}

foreach ($candidate in $Candidates) {
    $candidateSeenUrls = $SeenByCandidate[$candidate.Name]
    $linkedinJobs = @(Get-LinkedInJobs $candidate $candidateSeenUrls)
    foreach ($job in $linkedinJobs) { if ($job.url) { [void]$candidateSeenUrls.Add([string]$job.url) } }
    $remaining = [Math]::Max(0, $candidate.Max - $linkedinJobs.Count)
    $freehireJobs = @(Get-FreehireJobs $candidate $candidateSeenUrls $remaining)
    $jobs = @($linkedinJobs) + @($freehireJobs)
    $Summaries.Add("$($candidate.Name): $($jobs.Count)/$($candidate.Max) target")
    $Rows = [System.Collections.Generic.List[string]]::new()
    $Rows.Add("# Daily AI Job Search — $DisplayDate")
    $Rows.Add('')
    $Rows.Add("**Candidate: $($candidate.Name)**")
    $Rows.Add('')
    $Rows.Add("Found $($jobs.Count) new unique listings within the past 24 hours across LinkedIn and Freehire; daily target is $($candidate.Max). Actual results depend on fresh matches available from both sources.")
    $Rows.Add('')
    $Rows.Add($candidate.ExperienceNote)
    $Rows.Add('')
    $Rows.Add('Search cards do not reliably show salary or experience requirements. Review the linked posting for eligibility before applying.')
    $Rows.Add('')
    $Rows.Add('## Listings')
    $Rows.Add('')
    $Rows.Add('| # | Title | Employer | Salary | Experience | Location | Posted | Source |')
    $Rows.Add('|---:|---|---|---|---|---|---|---|')
    $index = 0
    foreach ($job in $jobs) {
        [void]$candidateSeenUrls.Add([string]$job.url)
        $index++
        $title = Escape-TableCell ([string]$job.title)
        $company = Escape-TableCell ([string]$job.company)
        $location = Escape-TableCell ([string]$job.location)
        $posted = Escape-TableCell ([string]$job.date)
        $source = if ($job.source) { [string]$job.source } else { 'LinkedIn' }
        $Rows.Add("| $index | [$title]($($job.url)) | $company | Not stated | Not stated | $location | $posted | $source |")
    }
    $Rows.Add('')
    $Path = Join-Path $ReportDir ("{0}_{1}.md" -f $Stamp, $candidate.Name)
    if (Test-Path -LiteralPath $Path) {
        $Path = Join-Path $ReportDir ("{0}_{1}_{2}.md" -f $Stamp, $candidate.Name, [guid]::NewGuid().ToString('N').Substring(0, 6))
    }
    [System.IO.File]::WriteAllLines($Path, $Rows, [System.Text.UTF8Encoding]::new($false))
}

New-Item -ItemType Directory -Force -Path (Join-Path $Repo 'outputs') | Out-Null
Add-Content -LiteralPath (Join-Path $Repo 'outputs\daily-job-refresh.log') -Value "$(Get-Date -Format o) $($Summaries -join '; ')"
