package com.abdownloadmanager.shared.downloaderinui.ytdlp

import com.abdownloadmanager.shared.downloaderinui.DownloadSize
import com.abdownloadmanager.shared.downloaderinui.LinkChecker
import ir.amirab.downloader.downloaditem.ytdlp.YtdlpDownloadCredentials
import ir.amirab.downloader.downloaditem.ytdlp.YtdlpProcessManager
import ir.amirab.downloader.downloaditem.ytdlp.YtdlpResponseInfo
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import java.io.BufferedReader
import java.io.InputStreamReader

class YtdlpLinkChecker(
    initialCredentials: YtdlpDownloadCredentials
) : LinkChecker<YtdlpDownloadCredentials, YtdlpResponseInfo, DownloadSize.Bytes>(
    initialCredentials
) {
    private val _suggestedName = MutableStateFlow<String?>(null)
    override val suggestedName: StateFlow<String?> = _suggestedName.asStateFlow()

    private val _downloadSize = MutableStateFlow<DownloadSize.Bytes?>(null)
    override val downloadSize: StateFlow<DownloadSize.Bytes?> = _downloadSize.asStateFlow()

    override fun infoUpdated(responseInfo: YtdlpResponseInfo?) {
        if (responseInfo != null) {
            val title = responseInfo.title ?: "video"
            val targetExt = responseInfo.ext ?: "mp4"
            _suggestedName.value = "$title.$targetExt"
            _downloadSize.value = if (responseInfo.size > 0) {
                DownloadSize.Bytes(responseInfo.size)
            } else {
                null
            }
        } else {
            _suggestedName.value = null
            _downloadSize.value = null
        }
    }

    override suspend fun actualCheck(credentials: YtdlpDownloadCredentials): YtdlpResponseInfo {
        credentials.validateCredentials()
        return withContext(Dispatchers.IO) {
            YtdlpProcessManager.ensureExecutableInstalled()
            val quality = credentials.quality
            val formatArgs = if (quality.audioOnly) {
                listOf("-f", "bestaudio/best")
            } else {
                listOf("-f", quality.toFormatSelector())
            }

            val args = mutableListOf(
                YtdlpProcessManager.getExePath(),
                "--ignore-config",
                "--no-plugin-dirs",
                "--no-remote-components",
                "--no-js-runtimes",
                "--dump-json",
                "--no-playlist"
            )
            args += YtdlpProcessManager.resolveJsRuntimeArgs()
            args += formatArgs
            args += listOf("--", credentials.link)

            val proc = ProcessBuilder(args).start()
            val stdout = BufferedReader(InputStreamReader(proc.inputStream)).use { it.readText() }
            val stderr = BufferedReader(InputStreamReader(proc.errorStream)).use { it.readText() }
            val exitCode = proc.waitFor()
            if (exitCode != 0) {
                throw Exception("yt-dlp failed with exit code $exitCode: ${stderr.trim()}")
            }

            val jsonObject = Json.parseToJsonElement(stdout).jsonObject
            val title = jsonObject["title"]?.jsonPrimitive?.content
            val requestedDownloads = jsonObject["requested_downloads"]?.jsonArray
            val size = requestedDownloads
                ?.sumOf {
                    it.jsonObject["filesize"]?.jsonPrimitive?.longOrNull
                        ?: it.jsonObject["filesize_approx"]?.jsonPrimitive?.longOrNull
                        ?: 0L
                }
                ?.takeIf { it > 0 }
                ?: (jsonObject["filesize"]?.jsonPrimitive?.longOrNull
                    ?: jsonObject["filesize_approx"]?.jsonPrimitive?.longOrNull
                    ?: -1L)

            val ext = if (quality.audioOnly) "mp3" else "mp4"
            YtdlpResponseInfo(
                isSuccessFul = true,
                title = title,
                ext = ext,
                size = size
            )
        }
    }
}
