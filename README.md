# Xiaomi Gateway Media & TTS Player (`xiaomi_gateway_player`)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Integração personalizada (Custom Component) para **Home Assistant** que transforma o altifalante interno da **Xiaomi Mi Control Hub (`DGNWG02LM` / `lumi.gateway.v3`)** num reprodutor `media_player` nativo 100% local, sem qualquer dependência dos servidores Cloud da Xiaomi.

Permite enviar **Text-to-Speech (TTS)**, links ou pesquisas diretas do **MyInstants**, ficheiros de áudio locais (`.mp3`, `.wav`, `.ogg`, `.aac`) e gerir os slots de memória flash interna da Gateway através de comandos UDP Miio encriptados (`AES-128-CBC`).

---

## ✨ Funcionalidades Principais

- **100% Local (Sem Cloud)**: Usa o protocolo local Miio (`UDP 54321`) com `download_user_music` + `play_music_new`, contornando a limitação da app Xiaomi Home que exigia ligação à cloud para carregar toques personalizados.
- **Suporte Nativo a TTS (`tts.speak`)**: Funciona diretamente com Google AI TTS, Google Translate TTS, Piper, ElevenLabs ou qualquer motor TTS do Home Assistant.
- **Integração Direta com MyInstants**:
  - **Por Pesquisa**: basta passar `"myinstants:vine boom"` no `media_content_id` e a integração pesquisa, extrai o primeiro MP3, converte e toca.
  - **Por URL da Página ou MP3**: aceita tanto o link da página do botão (`https://www.myinstants.com/pt/instant/...`) como o link direto `.mp3`.
- **Conversão Automática em Tempo Real (`ffmpeg`)**: Converte automaticamente qualquer fonte de áudio para o formato estrito exigido pelo SoC Marvell `MW300` da Gateway (**AAC ADTS, 22050 Hz, Mono, 32 kbps**).
- **Gestão de Cache em Memória Flash (`save_sound` / `delete_sound`)**: Permite gravar sons favoritos permanentemente nos slots `10001` a `10029` da memória flash da Gateway para reprodução instantânea (**$< 50\text{ ms}$**).
- **Volume Seguro por Defeito**: Inicia a `12%` (`0.12`) para evitar disparos acidentais no volume máximo do altifalante interno.

---

## ⚡ Latência e Limites de Hardware (`MW300`)

| Métrica | Valor Real Medido | Notas Técnicas |
| :--- | :--- | :--- |
| **Latência (Slot em Cache `10001`–`10029`)** | **$< 50\text{ ms}$** | Reprodução imediata a partir da flash interna (`play_music_new`). |
| **Latência (Som Dinâmico / MyInstants / TTS em Cache)** | **$\approx 470\text{–}490\text{ ms}$** | Inclui conversão `ffmpeg` em RAM (`/tmp`) + download HTTP local pela Gateway + flash no slot dinâmico `10030`. |
| **Espaço Total em Flash (User Music)** | **$\approx 11.6\text{ MB}$** | Exposto em tempo real no atributo `free_space_bytes`. |
| **Tamanho Máximo por Slot** | **$\approx 512\text{ KB}$** | Limite de firmware do comando `download_user_music`. |
| **Duração Máxima por Som** | **$\approx 110\text{–}120\text{ segundos}$** | A `32 kbps` mono AAC ADTS ($\approx 4\text{ KB/s}$), cabem até ~2 minutos de áudio por slot. |

---

## 📦 Instalação via HACS (Recomendado)

1. No Home Assistant, abrir **HACS** $\rightarrow$ menu de 3 pontos (canto superior direito) $\rightarrow$ **Repositórios personalizados (Custom repositories)**.
2. Adicionar o URL deste repositório:
   - **Repositório**: `https://github.com/dbarciela/xiaomi-gateway-player`
   - **Categoria**: `Integração (Integration)`
3. Clicar em **Transferir (Download)** na integração **Xiaomi Gateway Media & TTS Player**.
4. Reiniciar o Home Assistant.
5. Ir a **Definições $\rightarrow$ Dispositivos e Serviços $\rightarrow$ Adicionar Integração** e procurar por **Xiaomi Gateway Media & TTS Player**.

### Parâmetros de Configuração (UI Config Flow)

- **Endereço IP da Gateway (`host`)**: ex. `192.168.1.68`
- **Token Miio (`token`)**: Token hexadecimal de 32 caracteres da Gateway (obtido via Xiaomi Home mod by VEVS ou `xiaomi-cloud-tokens-extractor`).
- **URL HTTP Local do Home Assistant (`ha_url`)**: ex. `http://192.168.1.150:8123` *(tem de ser HTTP simples, pois o cliente de download do chip `MW300` não suporta TLS/HTTPS moderno)*.
- **Nome Amigável (`name`)**: ex. `Coluna Gateway Hall`

---

## 🚀 Exemplos de Utilização em Automações

### 1. Enviar Text-to-Speech (TTS)
```yaml
action: tts.speak
target:
  entity_id: tts.google_ai_tts
data:
  media_player_entity_id: media_player.hall_gateway
  message: "Atenção, a porta da garagem ficou aberta."
```

### 2. Tocar qualquer som do MyInstants (Pesquisa ou Link)
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

### 3. Guardar um som permanentemente num Slot da Flash (`10001` a `10029`)
Para sons usados frequentemente (campainha, alarme, avisos), guarda-os num slot fixo uma única vez:
```yaml
action: xiaomi_gateway_player.save_sound
data:
  url: "myinstants:vine boom"
  slot_id: "10006"
  play_now: true
  volume: 12
```

Depois, nas automações, basta chamar o slot para tocar instantaneamente ($< 50\text{ ms}$) sem precisar de internet:
```yaml
action: media_player.play_media
target:
  entity_id: media_player.hall_gateway
data:
  media_content_type: music
  media_content_id: "slot:10006"
```

### 4. Apagar um Slot da Memória Flash
```yaml
action: xiaomi_gateway_player.delete_sound
data:
  slot_id: "10006"
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
