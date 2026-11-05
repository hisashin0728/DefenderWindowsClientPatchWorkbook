#requires -Modules Microsoft.Graph.Applications
<#
Logic App のマネージド ID に Microsoft Graph の ThreatHunting.Read.All (アプリケーション権限) を付与する。
ARM/Bicep では付与できないため、グローバル管理者または特権ロール管理者で実行する。付与済みなら変更しない。

例: .\grant-defender-api-permission.ps1 -LogicAppPrincipalId <Logic App のマネージド ID のオブジェクト ID>
#>
param(
    [Parameter(Mandatory)] [string] $LogicAppPrincipalId
)

$ErrorActionPreference = 'Stop'

Connect-MgGraph -Scopes 'AppRoleAssignment.ReadWrite.All', 'Application.Read.All' -NoWelcome

$target = Get-MgServicePrincipal -ServicePrincipalId $LogicAppPrincipalId `
    -Property 'Id', 'DisplayName', 'ServicePrincipalType'
if ($target.ServicePrincipalType -ne 'ManagedIdentity') {
    throw '対象は Logic App のマネージド ID を指定してください。'
}

$resources = @(Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'" `
    -Property 'Id', 'AppRoles')
if ($resources.Count -ne 1) { throw 'Microsoft Graph のサービスプリンシパルを一意に取得できません。' }
$graph = $resources[0]
$roles = @($graph.AppRoles | Where-Object {
    $_.Value -eq 'ThreatHunting.Read.All' -and $_.IsEnabled -and
    'Application' -in $_.AllowedMemberTypes
})
if ($roles.Count -ne 1) { throw 'ThreatHunting.Read.All のアプリケーション権限が見つかりません。' }

$assigned = Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $target.Id -All |
    Where-Object { $_.ResourceId -eq $graph.Id -and $_.AppRoleId -eq $roles[0].Id }
if ($assigned) {
    Write-Host 'ThreatHunting.Read.All は付与済みです。'
    return
}

New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $target.Id `
    -PrincipalId $target.Id -ResourceId $graph.Id -AppRoleId $roles[0].Id | Out-Null
Write-Host 'ThreatHunting.Read.All を付与しました。'
