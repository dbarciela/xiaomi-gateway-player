<#
.SYNOPSIS
    Converte um ficheiro de áudio (MP3/WAV) para AAC ADTS (22050 Hz, Mono, 32 kbps)
    e envia-o localmente via protocolo Miio UDP para um slot da Xiaomi Gateway v3 (DGNWG02LM).

.EXAMPLE
    .\upload_gateway_ringtone.ps1 -GatewayIp "192.168.1.68" -Token "<32_HEX_TOKEN>" -HaIp "192.168.1.150" -InputMp3 ".\som.mp3" -SlotId 10005 -TestVolume 12
#>
param(
    [Parameter(Mandatory=$true)][string]$GatewayIp,
    [Parameter(Mandatory=$true)][string]$Token,
    [Parameter(Mandatory=$true)][string]$HaIp,
    [Parameter(Mandatory=$true)][string]$InputMp3,
    [Parameter(Mandatory=$false)][string]$WwwPath = "h:\www",
    [Parameter(Mandatory=$false)][int]$SlotId = 10005,
    [Parameter(Mandatory=$false)][int]$TestVolume = 12
)

if ($SlotId -lt 10001 -or $SlotId -gt 10030) {
    throw "O SlotId deve estar entre 10001 e 10030."
}

New-Item -ItemType Directory -Force -Path $WwwPath | Out-Null
$aacName = "ringtone_$SlotId.aac"
$aacPath = Join-Path $WwwPath $aacName

Write-Host "1. A converter '$InputMp3' para AAC ADTS ($aacPath)..."
ffmpeg -y -i $InputMp3 -t 110 -vn -ac 1 -ar 22050 -c:a aac -b:a 32k -f adts $aacPath -loglevel warning

$tokenBytes = [Convert]::FromHexString($Token)
$md5 = [System.Security.Cryptography.MD5]::Create()
$key = $md5.ComputeHash($tokenBytes)
$ivIn = [byte[]]::new(32)
[Array]::Copy($key, 0, $ivIn, 0, 16)
[Array]::Copy($tokenBytes, 0, $ivIn, 16, 16)
$iv = $md5.ComputeHash($ivIn)

$udp = New-Object System.Net.Sockets.UdpClient
$udp.Client.ReceiveTimeout = 5000
$ep = New-Object System.Net.IPEndPoint ([System.Net.IPAddress]::Parse($GatewayIp), 54321)

$hello = [Convert]::FromHexString("21310020ffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
[void]$udp.Send($hello, 32, $ep)
$resp = $udp.Receive([ref]$ep)
$stamp = [uint32](([int]$resp[12] -shl 24) -bor ([int]$resp[13] -shl 16) -bor ([int]$resp[14] -shl 8) -bor [int]$resp[15])
$msgId = [int]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() % 50000) + 10000

function Invoke-Miio($method, $paramsJson) {
    $script:stamp++
    $script:msgId++
    $json = "{`"id`":$($script:msgId),`"method`":`"$method`",`"params`":$paramsJson}`0"
    $plain = [System.Text.Encoding]::UTF8.GetBytes($json)
    $aes = [System.Security.Cryptography.Aes]::Create()
    $aes.Mode = [System.Security.Cryptography.CipherMode]::CBC
    $aes.Padding = [System.Security.Cryptography.PaddingMode]::PKCS7
    $aes.Key = $key; $aes.IV = $iv
    $enc = $aes.CreateEncryptor().TransformFinalBlock($plain, 0, $plain.Length)
    $len = [uint16](32 + $enc.Length)
    $pkt = [byte[]]::new($len)
    $pkt[0] = 0x21; $pkt[1] = 0x31
    $pkt[2] = [byte](([int]$len -shr 8) -band 0xff); $pkt[3] = [byte]([int]$len -band 0xff)
    [Array]::Copy($resp, 8, $pkt, 8, 4)
    $pkt[12] = [byte](([int]$script:stamp -shr 24) -band 0xff)
    $pkt[13] = [byte](([int]$script:stamp -shr 16) -band 0xff)
    $pkt[14] = [byte](([int]$script:stamp -shr 8) -band 0xff)
    $pkt[15] = [byte]([int]$script:stamp -band 0xff)
    [Array]::Copy($tokenBytes, 0, $pkt, 16, 16)
    [Array]::Copy($enc, 0, $pkt, 32, $enc.Length)
    $chk = $md5.ComputeHash($pkt)
    [Array]::Copy($chk, 0, $pkt, 16, 16)
    [void]$udp.Send($pkt, $pkt.Length, $ep)
    $r = $udp.Receive([ref]$ep)
    $dec = $aes.CreateDecryptor().TransformFinalBlock($r, 32, $r.Length - 32)
    return [System.Text.Encoding]::UTF8.GetString($dec).TrimEnd("`0")
}

$url = "http://$($HaIp):8123/local/$aacName"
Write-Host "2. A enviar '$url' para o slot $SlotId da Gateway ($GatewayIp)..."
Write-Host "   Resposta:" (Invoke-Miio "download_user_music" "[`"$SlotId`",`"$url`"]")

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 100
    $p = Invoke-Miio "get_download_progress" "[]"
    if ($p -match ":100") {
        Write-Host "   Progresso concluído: $p"
        break
    }
}

if ($TestVolume -gt 0) {
    Write-Host "3. A reproduzir slot $SlotId com volume $TestVolume..."
    Write-Host "   Resposta:" (Invoke-Miio "play_music_new" "[`"$SlotId`",$TestVolume]")
}

$udp.Close()
