# Xiaomi Gateway Media, Radio & TTS Player (`xiaomi_gateway_player`)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Integração personalizada (Custom Component) para **Home Assistant** que transforma o altifalante interno da **Xiaomi Mi Control Hub (`DGNWG02LM` / `lumi.gateway.v3`)** num reprodutor `media_player` nativo 100% local, sem qualquer dependência dos servidores Cloud da Xiaomi ou de proxies externos instáveis.

Permite tocar **Rádios Online de todo o mundo (via Radio Browser API)**, **Álbuns e Playlists completas via Subsonic / Navidrome API**, **qualquer playlist `.m3u` / `.pls` ou stream contínuo**, **Text-to-Speech (TTS) de qualquer duração**, links/pesquisas do **MyInstants** e gerir os slots de memória flash interna da Gateway através de comandos UDP Miio encriptados (`AES-128-CBC`).

---

## ✨ Funcionalidades Principais

- **📻 Rádios Online de Todo o Mundo (Radio Browser + Proxy HLS Local)**:
  - Integração direta com o diretório comunitário mundial gratuito **[Radio Browser](https://www.radio-browser.info/)** (+40.000 estações de rádio em todo o mundo).
  - **Filtro por País Configurável**: deteta automaticamente o país configurado no Home Assistant (ex: `PT`, `BR`, `ES`, `GB`, `US`, `DE`) e permite alterá-lo nas opções da integração sem bloquear o projeto a um único país.
  - **Seletor de Fonte (`source_list`) Personalizável**: escolhe diretamente no cartão do Media Player as tuas estações favoritas (ex: *Antena 1, TSF, Rádio Nova Era, Smooth FM, Rádio Comercial, RFM* ou qualquer estação mundial/URL personalizado `Nome|URL`). Se deixado em branco noutros países, carrega automaticamente o Top 8 das rádios mais ouvidas desse país.
- **💿 Álbuns Completos, Playlists `.m3u`/`.pls` & Integração Subsonic / Navidrome**:
  - **Subsonic / Navidrome API Nativa**: configura opcionalmente o teu servidor Subsonic/Navidrome nas opções da integração e toca álbuns inteiros (`subsonic:album:...`), artistas (`subsonic:artist:...`), músicas (`subsonic:song:...`), playlists (`subsonic:playlist:...`) ou mixes aleatórios (`subsonic:random:25`).
  - **Suporte a Playlists Multi-Faixa (`.m3u` / `.m3u8` / `.pls`)**: extrai automaticamente todas as faixas de qualquer ficheiro/URL `.m3u` ou `.pls` e transmite o álbum inteiro sem interrupções através do pipeline `ffconcat` $\rightarrow$ HLS (`-readrate 1.0 -readrate_initial_burst 6`), parando automaticamente a Gateway quando a última faixa termina!
- **🗣️ Suporte Nativo a TTS Curto e Longo (`tts.speak`)**:
  - **Modo Híbrido Automático**: mensagens TTS curtas ($\le 105\text{ s}$) usam o slot dinâmico para arranque em $\approx 480\text{ ms}$; mensagens **TTS longas ($> 105\text{ s}$, resumos matinais de IA, notícias ou podcasts)** comutam **automaticamente** para o motor de streaming HLS (`play_specify_fm`), eliminando o limite de 512 KB da memória flash!
- **🔊 Integração Direta com MyInstants**:
  - **Por Pesquisa**: basta passar `"myinstants:vine boom"` no `media_content_id` e a integração pesquisa, extrai o primeiro MP3, converte e toca.
  - **Por URL da Página ou MP3**: aceita tanto o link da página do botão (`https://www.myinstants.com/pt/instant/...`) como o link direto `.mp3`.
- **💾 Gestão de Cache em Memória Flash (`save_sound` / `delete_sound`)**: Permite gravar sons favoritos permanentemente nos slots `10001` a `10029` da memória flash da Gateway para reprodução instantânea (**$< 50\text{ ms}$**).
- **🛡️ Volume Seguro & Controlo em Tempo Real**: Inicia a `12%` (`0.12`) e ajusta o volume do stream HLS/Rádio em tempo real (`volume_ctrl_fm`) ao mover o *slider* no Home Assistant.

---

## ⚡ Arquitetura Híbrida: Slot Flash vs. Motor HLS (`MW300`)

| Modo de Reprodução | Comando Miio | Latência de Arranque | Limite de Duração | Desgaste de Flash | Quando é usado? |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. Slot Flash Fixo (`10001`–`10029`)** | `play_music_new` | **$< 50\text{ ms}$** | $\approx 120\text{ s}$ ($512\text{ KB}$) | Zero na leitura | `slot:10005`, `ringtone:0` (sons guardados com `save_sound`). |
| **2. Slot Flash Dinâmico (`10030`)** | `download_user_music` + `play_music_new` | **$\approx 470\text{–}490\text{ ms}$** | $\le 105\text{ s}$ ($\le 430\text{ KB}$) | Escrita em flash | TTS curtos, efeitos MyInstants e clipes curtos. |
| **3. Streaming HLS Contínuo (`gw_radio.m3u8`)** | `play_specify_fm` + `volume_ctrl_fm` | **$\approx 0.6\text{–}1.5\text{ s}$** (`burst 6s`) | **Ilimitado ($\infty$)** | **Zero (100% RAM/HTTP)** | Rádios online, Álbuns Subsonic, Playlists `.m3u`/`.pls`, `stream:<url>` e **qualquer TTS/música $> 105\text{ s}$ (automático)**. |

---

## 📦 Instalação via HACS (Recomendado)

1. No Home Assistant, abrir **HACS** $\rightarrow$ menu de 3 pontos (canto superior direito) $\rightarrow$ **Repositórios personalizados (Custom repositories)**.
2. Adicionar o URL deste repositório:
   - **Repositório**: `https://github.com/dbarciela/xiaomi-gateway-player`
   - **Categoria**: `Integração (Integration)`
3. Clicar em **Transferir (Download)** na integração **Xiaomi Gateway Media & TTS Player**.
4. Reiniciar o Home Assistant.
5. Ir a **Definições $\rightarrow$ Dispositivos e Serviços $\rightarrow$ Adicionar Integração** e procurar por **Xiaomi Gateway Media & TTS Player**.

### Parâmetros de Configuração e Opções (UI Config & Options Flow)

Podes configurar inicialmente ou alterar a qualquer momento clicando na roda dentada (**Configurar**) da integração:
- **Endereço IP da Gateway (`host`)**: ex. `192.168.1.68`
- **Token Miio (`token`)**: Token hexadecimal de 32 caracteres da Gateway.
- **URL HTTP Local do Home Assistant (`ha_url`)**: ex. `http://192.168.1.150:8123` *(tem de ser HTTP simples, pois o cliente de download do chip `MW300` não suporta TLS/HTTPS moderno)*.
- **Código ISO do País para Rádios (`radio_country`)**: Código de 2 letras (ex: `PT`, `BR`, `ES`, `GB`, `US`, `DE`). Preenche automaticamente com o país do teu Home Assistant.
- **Rádios Favoritas (`favorite_radios`)**: Lista separada por vírgulas com os nomes das estações para aparecerem no seletor **Fonte (Source)** do Media Player (ex: `Antena 1, TSF, Rádio Nova Era, Smooth FM, Rádio Comercial, RFM`), ou pares `Nome|URL` para streams personalizados.
- **Servidor Subsonic / Navidrome (`subsonic_url`, `subsonic_user`, `subsonic_password`)** *(Opcional)*: URL base (ex: `http://192.168.1.100:4533`), utilizador e password para pesquisar e tocar álbuns/músicas diretamente da tua biblioteca musical.

---

## 🚀 Exemplos de Utilização

### 1. Tocar Rádios Online (Seletor de Fonte, Pesquisa ou Media Browser)

```yaml
# A. Escolher uma estação favorita do Seletor de Fonte (Source)
action: media_player.select_source
target:
  entity_id: media_player.hall_gateway
data:
  source: "Smooth FM"

# B. Pesquisa direta no Radio Browser no país configurado (ex: PT)
action: media_player.play_media
target:
  entity_id: media_player.hall_gateway
data:
  media_content_type: channel
  media_content_id: "radio:Rádio Nova Era"

# C. Pesquisa forçando outro país com [XX]
action: media_player.play_media
target:
  entity_id: media_player.hall_gateway
data:
  media_content_type: channel
  media_content_id: "radio:BBC Radio 1 [GB]"
```

---

### 2. Álbuns Completos via Subsonic / Navidrome ou Playlists `.m3u`

```yaml
# Tocar um álbum completo do teu servidor Subsonic / Navidrome
action: media_player.play_media
target:
  entity_id: media_player.hall_gateway
data:
  media_content_type: music
  media_content_id: "subsonic:album:Random Access Memories"

# Tocar todas as músicas de um artista ou uma playlist do Subsonic
action: media_player.play_media
target:
  entity_id: media_player.hall_gateway
data:
  media_content_type: music
  media_content_id: "subsonic:artist:Daft Punk"

# Tocar um mix aleatório de 25 músicas da tua biblioteca Subsonic
action: media_player.play_media
target:
  entity_id: media_player.hall_gateway
data:
  media_content_type: music
  media_content_id: "subsonic:random:25"

# Tocar qualquer playlist .m3u / .pls ou stream contínuo
action: media_player.play_media
target:
  entity_id: media_player.hall_gateway
data:
  media_content_type: playlist
  media_content_id: "http://192.168.1.100/musica/album.m3u"
```

---

### 3. Enviar Text-to-Speech (TTS Curto ou Longo)
Textos curtos tocam pelo slot rápido; textos longos ($> 105\text{ s}$) passam automaticamente para streaming HLS sem cortar uma única palavra:
```yaml
action: tts.speak
target:
  entity_id: tts.google_ai_tts
data:
  media_player_entity_id: media_player.hall_gateway
  message: "Bom dia! Aqui está o resumo completo da tua agenda e das notícias de hoje..."
```

---

### 4. Tocar qualquer som do MyInstants (Pesquisa ou Link)
```yaml
# Pesquisa direta no MyInstants (toca o 1º resultado)
action: media_player.play_media
target:
  entity_id: media_player.hall_gateway
data:
  media_content_type: music
  media_content_id: "myinstants:vine boom"

# Ou usando o URL da página do botão no MyInstants
action: media_player.play_media
target:
  entity_id: media_player.hall_gateway
data:
  media_content_type: music
  media_content_id: "https://www.myinstants.com/pt/instant/metal-gear-solid-alert/"
```

---

### 5. Guardar um som permanentemente num Slot da Flash (`10001` a `10029`)
```yaml
action: xiaomi_gateway_player.save_sound
data:
  url: "myinstants:vine boom"
  slot_id: "10006"
  play_now: true
  volume: 12
```
Para reproduzir instantaneamente ($< 50\text{ ms}$):
```yaml
action: media_player.play_media
target:
  entity_id: media_player.hall_gateway
data:
  media_content_type: music
  media_content_id: "slot:10006"
```

---

## 🛠️ Script Utilitário PowerShell (CLI Standalone)

Na pasta [`scripts/upload_gateway_ringtone.ps1`](scripts/upload_gateway_ringtone.ps1) está incluído um script autónomo em PowerShell para converter e enviar ficheiros MP3 para a Gateway diretamente a partir do PC:

```powershell
.\scripts\upload_gateway_ringtone.ps1 `
  -GatewayIp "192.168.1.68" `
  -Token "<32_HEX_TOKEN>" `
  -HaIp "192.168.1.150" `
  -InputMp3 ".\meu_som.mp3" `
  -SlotId 10005 `
  -TestVolume 12
```

---

## 📄 Licença

Distribuído sob a licença [MIT](LICENSE).
