package ir.amirab.downloader.downloaditem.ytdlp

import ir.amirab.downloader.DownloadManager
import ir.amirab.downloader.destination.DownloadDestination
import ir.amirab.downloader.destination.SimpleDownloadDestination
import ir.amirab.downloader.downloaditem.DownloadJob
import ir.amirab.downloader.downloaditem.DownloadJobExtraConfig
import ir.amirab.downloader.downloaditem.DownloadJobStatus
import ir.amirab.downloader.downloaditem.DownloadStatus
import ir.amirab.downloader.downloaditem.IDownloadItem
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancel
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import java.util.concurrent.atomic.AtomicLong

class YtdlpDownloadJob(
    override val downloadItem: YtdlpDownloadItem,
    downloadManager: DownloadManager,
    private val ytdlpExecutablePathProvider: () -> String,
) : DownloadJob(
    downloadManager = downloadManager,
) {
    private lateinit var destination: SimpleDownloadDestination
    private var process: Process? = null
    private val downloadedBytes = AtomicLong(0L)

    override fun getDestination(): DownloadDestination = destination

    override suspend fun actualBoot() {
        initializeDestination()
    }

    override fun initializeDestination() {
        val outFile = downloadManager.calculateOutputFile(downloadItem)
        destination = SimpleDownloadDestination(
            file = outFile,
            emptyFileCreator = downloadManager.emptyFileCreator,
            appendExtensionToIncompleteDownloads = downloadManager.settings.appendExtensionToIncompleteDownloads,
            downloadId = id
        )
    }

    override suspend fun reset() {
        pause()
        downloadItem.contentLength = -1L
        downloadItem.status = DownloadStatus.Added
        downloadItem.startTime = null
        downloadItem.completeTime = null
        downloadedBytes.set(0)
        saveState()
        downloadManager.onDownloadItemChange(downloadItem)
    }

    override suspend fun resume() {
        if (isDownloadActive.value) return
        _isDownloadActive.update { true }

        val activeScope = newScopeBasedOn(scope)
        activeDownloadScope = activeScope

        activeScope.launch {
            boot()
            onDownloadResuming()
            try {
                downloadItem.validateCredentials()
                _status.value = DownloadJobStatus.PreparingFile(0)
                val exeFile = YtdlpProcessManager.ensureExecutableInstalled { progress ->
                    _status.value = DownloadJobStatus.PreparingFile((progress * 100).toInt())
                }
                val exePath = exeFile.absolutePath

                val isAudioOnly = downloadItem.quality.audioOnly
                val targetExtension = if (isAudioOnly) "mp3" else "mp4"
                val currentName = downloadItem.name
                val currentExt = currentName.substringAfterLast('.', "")
                if (currentExt.lowercase() != targetExtension) {
                    val baseName = if (currentExt.isEmpty()) currentName else currentName.substringBeforeLast('.')
                    downloadItem.name = "$baseName.$targetExtension"
                    saveState()
                    initializeDestination()
                }

                File(downloadItem.folder).mkdirs()
                downloadItem.status = DownloadStatus.Downloading
                if (downloadItem.startTime == null) {
                    downloadItem.startTime = System.currentTimeMillis()
                }
                saveState()
                onDownloadResumed()

                withContext(Dispatchers.IO) {
                    val ffmpegLocationDir = YtdlpProcessManager.resolveFfmpegLocationArg()
                    val outputTemplate = File(downloadItem.folder, downloadItem.name).absolutePath

                    val args = mutableListOf(
                        exePath,
                        "--ignore-config",
                        "--no-plugin-dirs",
                        "--no-remote-components",
                        "--no-js-runtimes"
                    )
                    args += YtdlpProcessManager.resolveJsRuntimeArgs()
                    if (ffmpegLocationDir != null) {
                        args += listOf("--ffmpeg-location", ffmpegLocationDir.absolutePath)
                    }
                    args += if (isAudioOnly) {
                        listOf(
                            "-f", "bestaudio/best",
                            "-x", "--audio-format", "mp3",
                            "--newline",
                            "--progress",
                            "--no-playlist",
                            "-o", outputTemplate,
                            "--",
                            downloadItem.link
                        )
                    } else {
                        listOf(
                            "-f", downloadItem.quality.toFormatSelector(),
                            "--remux-video", "mp4",
                            "--merge-output-format", "mp4",
                            "--newline",
                            "--progress",
                            "--no-playlist",
                            "-o", outputTemplate,
                            "--",
                            downloadItem.link
                        )
                    }

                    val proc = ProcessBuilder(args)
                        .redirectErrorStream(true)
                        .start()
                    process = proc

                    val percentRegex = """([\d.]+)%""".toRegex()
                    val sizeRegex = """of\s+~?([\d.]+)(\w+)""".toRegex()
                    val outputLog = mutableListOf<String>()

                    BufferedReader(InputStreamReader(proc.inputStream)).use { reader ->
                        while (true) {
                            val line = reader.readLine() ?: break
                            ensureActive()
                            outputLog += line
                            if (outputLog.size > 100) outputLog.removeAt(0)

                            val percentMatch = percentRegex.find(line) ?: continue
                            val percent = percentMatch.groupValues[1].toDoubleOrNull() ?: continue
                            val sizeMatch = sizeRegex.find(line) ?: continue
                            val totalBytes = parseSizeToBytes(
                                sizeMatch.groupValues[1],
                                sizeMatch.groupValues[2]
                            )
                            if (totalBytes > 0) {
                                if (downloadItem.contentLength != totalBytes) {
                                    downloadItem.contentLength = totalBytes
                                    saveState()
                                }
                                downloadedBytes.set((percent * totalBytes / 100.0).toLong())
                            }
                        }
                    }

                    val exitCode = proc.waitFor()
                    if (exitCode != 0) {
                        throw Exception(
                            "yt-dlp exited with code $exitCode. Output:\n${outputLog.joinToString("\n")}"
                        )
                    }
                    if (!File(outputTemplate).exists()) {
                        throw Exception(
                            "yt-dlp finished but the expected output file was not created. Output:\n" +
                                outputLog.joinToString("\n")
                        )
                    }
                }
                onDownloadFinished()
            } catch (error: Exception) {
                onDownloadCanceled(error)
            } finally {
                process?.destroy()
                process = null
            }
        }
    }

    private fun parseSizeToBytes(valueStr: String, unit: String): Long {
        val value = valueStr.toDoubleOrNull() ?: return -1
        return when (unit.uppercase()) {
            "GB", "GIB" -> (value * 1024 * 1024 * 1024).toLong()
            "MB", "MIB" -> (value * 1024 * 1024).toLong()
            "KB", "KIB" -> (value * 1024).toLong()
            "B" -> value.toLong()
            else -> -1
        }
    }

    override suspend fun pause(throwable: Throwable) {
        activeDownloadScope?.cancel()
        process?.destroy()
        process = null
        _isDownloadActive.update { false }
        onDownloadCanceled(throwable)
    }

    override suspend fun saveState() {
        downloadManager.dlListDb.update(downloadItem)
    }

    override fun getDownloadedSize(): Long = downloadedBytes.get()

    override fun reloadSettings() = Unit

    override suspend fun changeConfig(
        updater: (IDownloadItem) -> Unit,
        extraConfig: DownloadJobExtraConfig?
    ): IDownloadItem {
        updater(downloadItem)
        saveState()
        return downloadItem
    }

    override suspend fun extraConfigsReceived(config: DownloadJobExtraConfig) = Unit
}
