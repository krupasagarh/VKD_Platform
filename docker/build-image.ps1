# Build the VK Platform image on this laptop and (optionally) save it as a .tar
# you can copy to any server and `docker load -i vk-platform.tar`.
#
#   powershell -ExecutionPolicy Bypass -File docker\build-image.ps1
#   powershell -ExecutionPolicy Bypass -File docker\build-image.ps1 -Save
#   powershell -ExecutionPolicy Bypass -File docker\build-image.ps1 -Push -Registry docker.io/krupasagarh
param(
    [string]$Tag = "latest",
    [switch]$Save,
    [switch]$Push,
    [string]$Registry = ""
)
$ErrorActionPreference = "Stop"
$hub = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$image = "vk-platform:$Tag"

Push-Location $hub
try {
    docker build -f vk_platform/Dockerfile -t $image .
    if ($LASTEXITCODE -ne 0) { throw "docker build failed" }
    if ($Save) {
        $out = Join-Path $hub "vk_platform\vk-platform-$Tag.tar"
        docker save -o $out $image
        Write-Host "Saved $out  ->  copy to the server, then: docker load -i vk-platform-$Tag.tar"
    }
    if ($Push) {
        if (-not $Registry) { throw "-Push needs -Registry (e.g. docker.io/yourname)" }
        $remote = "$Registry/vk-platform:$Tag"
        docker tag $image $remote
        docker push $remote
        Write-Host "Pushed $remote"
    }
} finally {
    Pop-Location
}
