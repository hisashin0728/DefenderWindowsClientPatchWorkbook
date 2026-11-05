#requires -Version 7.0
param(
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [Parameter(Mandatory)] [Alias('AutomationPrincipalId')] [string] $LogicAppPrincipalId
)

$ErrorActionPreference = 'Stop'
$token = az account get-access-token --subscription $SubscriptionId `
    --resource https://graph.microsoft.com/ --query accessToken --output tsv
if ($LASTEXITCODE -ne 0 -or -not $token) {
    throw 'Microsoft Graph authentication failed. Run az login with an authorized administrator.'
}
$headers = @{ Authorization = "Bearer $token" }
$target = Invoke-RestMethod `
    -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$LogicAppPrincipalId`?`$select=id,displayName,servicePrincipalType" `
    -Headers $headers
if ($target.servicePrincipalType -ne 'ManagedIdentity') {
    throw 'The target must be a Logic App managed identity.'
}
$resources = Invoke-RestMethod `
    -Uri 'https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId%20eq%20%2700000003-0000-0000-c000-000000000000%27&$select=id,appRoles' `
    -Headers $headers
if (@($resources.value).Count -ne 1) { throw 'Microsoft Graph service principal is ambiguous.' }
$graph = $resources.value[0]
$roles = @($graph.appRoles | Where-Object {
    $_.value -eq 'WindowsUpdates.Read.All' -and $_.isEnabled -and
    'Application' -in $_.allowedMemberTypes
})
if ($roles.Count -ne 1) { throw 'WindowsUpdates.Read.All application permission is unavailable.' }

$uri = "https://graph.microsoft.com/v1.0/servicePrincipals/$LogicAppPrincipalId/appRoleAssignments"
do {
    $page = Invoke-RestMethod -Uri $uri -Headers $headers
    if ($page.value | Where-Object {
        $_.resourceId -eq $graph.id -and $_.appRoleId -eq $roles[0].id
    }) {
        Write-Host "WindowsUpdates.Read.All is already assigned to $($target.displayName)."
        return
    }
    $uri = $page.'@odata.nextLink'
    if ($uri -and -not $uri.StartsWith('https://graph.microsoft.com/v1.0/')) {
        throw 'Unexpected Graph pagination URL.'
    }
} while ($uri)

$body = @{
    principalId = $LogicAppPrincipalId
    resourceId = $graph.id
    appRoleId = $roles[0].id
} | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post `
    -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$LogicAppPrincipalId/appRoleAssignments" `
    -Headers $headers -ContentType 'application/json' -Body $body | Out-Null
Write-Host "Assigned only WindowsUpdates.Read.All to $($target.displayName)."
